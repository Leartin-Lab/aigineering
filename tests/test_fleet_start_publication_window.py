from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import fcntl
from pathlib import Path
import threading

import pytest

from aigineering.core.sqlite_store import SQLiteStore
from aigineering.fleet_service import (
    FleetServiceError,
    read_service_metadata,
    start_service,
    stop_service,
)
from aigineering.local_identity import ensure_local_domain
import aigineering.fleet_service as fleet_service_module
from test_fleet_service import _enable_fixture_factory_import


def _prepare_public_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="service-worker"\nkind="local"\n'
        'worker_factory="fixtures.fleet_service_worker:build_worker"\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)
    return config, tmp_path / ".aig" / "fleet-service" / "state.json"


def _observe_publication_window(monkeypatch, state_path, *, fail_initial_write=False):
    metadata_write_entered = threading.Event()
    release_metadata_write = threading.Event()
    lock_contended = threading.Event()
    process_launches: list[object] = []
    initial_payload: dict[str, object] = {}
    original_write = fleet_service_module.write_service_metadata
    original_flock = fleet_service_module.fcntl.flock
    original_popen = fleet_service_module.subprocess.Popen

    def blocked_initial_metadata_write(path, payload):
        if Path(path) == state_path and not metadata_write_entered.is_set():
            initial_payload.update(payload)
            metadata_write_entered.set()
            if not release_metadata_write.wait(timeout=10):
                raise TimeoutError("test did not release initial metadata publication")
            if fail_initial_write:
                raise OSError("injected initial metadata publication failure")
        return original_write(path, payload)

    def observed_flock(descriptor, operation):
        try:
            return original_flock(descriptor, operation)
        except BlockingIOError:
            if metadata_write_entered.is_set() and operation & fcntl.LOCK_NB:
                lock_contended.set()
            raise

    def counted_popen(*args, **kwargs):
        process_launches.append(args[0])
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(
        fleet_service_module, "write_service_metadata", blocked_initial_metadata_write
    )
    monkeypatch.setattr(fleet_service_module.fcntl, "flock", observed_flock)
    monkeypatch.setattr(fleet_service_module.subprocess, "Popen", counted_popen)
    return (
        metadata_write_entered,
        release_metadata_write,
        lock_contended,
        process_launches,
        initial_payload,
        original_write,
    )


def test_concurrent_public_start_waits_for_initial_metadata_publication(
    tmp_path, monkeypatch
):
    config, state_path = _prepare_public_start(tmp_path, monkeypatch)
    (
        metadata_write_entered,
        release_metadata_write,
        lock_contended,
        process_launches,
        _,
        _,
    ) = _observe_publication_window(monkeypatch, state_path)

    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(start_service, tmp_path, config, readiness_timeout=10)
    second = None
    try:
        assert metadata_write_entered.wait(timeout=5), (
            f"first start failed before metadata publication: {first.exception()!r}"
        )
        second = pool.submit(start_service, tmp_path, config, readiness_timeout=10)
        assert lock_contended.wait(timeout=5)

        # Keep the valid first start inside its publication window. A duplicate
        # must wait for that start, not classify the missing initial snapshot as
        # an unsafe service state.
        with pytest.raises(TimeoutError):
            second.result(timeout=0.1)

        release_metadata_write.set()
        first_result = first.result(timeout=20)
        second_result = second.result(timeout=20)
        assert first_result.generation is not None
        assert second_result.generation == first_result.generation
        assert len(process_launches) == 1
        state = read_service_metadata(state_path)
        assert state is not None
        assert state["generation"] == first_result.generation
        assert state["state"] in {"ready", "starting"}
    finally:
        release_metadata_write.set()
        try:
            if not first.done():
                first.result(timeout=20)
            if second is not None and not second.done():
                second.result(timeout=20)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            stop_service(tmp_path, wait_timeout=10)


@pytest.mark.parametrize("readiness_timeout", [0.05, 0.0, float("nan"), float("inf")])
def test_missing_metadata_contention_stays_bounded(
    tmp_path, monkeypatch, readiness_timeout
):
    config, state_path = _prepare_public_start(tmp_path, monkeypatch)
    metadata_write_entered, release, contended, _, _, _ = _observe_publication_window(
        monkeypatch, state_path
    )
    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(start_service, tmp_path, config, readiness_timeout=10)
    second = None
    try:
        assert metadata_write_entered.wait(timeout=5), (
            f"first start failed before metadata publication: {first.exception()!r}"
        )
        second = pool.submit(
            start_service, tmp_path, config, readiness_timeout=readiness_timeout
        )
        assert contended.wait(timeout=5)
        with pytest.raises(FleetServiceError) as raised:
            second.result(timeout=2)
        assert raised.value.code == "unsafe_state"
    finally:
        release.set()
        try:
            if not first.done():
                first.result(timeout=20)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            stop_service(tmp_path, wait_timeout=10)


def test_invalid_metadata_during_contention_fails_closed_immediately(
    tmp_path, monkeypatch
):
    config, state_path = _prepare_public_start(tmp_path, monkeypatch)
    metadata_write_entered, release, contended, _, payload, original_write = (
        _observe_publication_window(monkeypatch, state_path)
    )
    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(start_service, tmp_path, config, readiness_timeout=10)
    second = None
    try:
        assert metadata_write_entered.wait(timeout=5), (
            f"first start failed before metadata publication: {first.exception()!r}"
        )
        second = pool.submit(start_service, tmp_path, config, readiness_timeout=10)
        assert contended.wait(timeout=5)
        invalid = dict(payload)
        invalid["workspace"] = str(tmp_path / "different-workspace")
        original_write(state_path, invalid)
        with pytest.raises(FleetServiceError) as raised:
            second.result(timeout=2)
        assert raised.value.code == "unsafe_state"
    finally:
        release.set()
        try:
            if not first.done():
                first.result(timeout=20)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            stop_service(tmp_path, wait_timeout=10)


def test_failed_initial_metadata_publication_releases_lock_for_waiting_start(
    tmp_path, monkeypatch
):
    config, state_path = _prepare_public_start(tmp_path, monkeypatch)
    metadata_write_entered, release, contended, process_launches, _, _ = (
        _observe_publication_window(monkeypatch, state_path, fail_initial_write=True)
    )
    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(start_service, tmp_path, config, readiness_timeout=10)
    second = None
    try:
        assert metadata_write_entered.wait(timeout=5), (
            f"first start failed before metadata publication: {first.exception()!r}"
        )
        second = pool.submit(start_service, tmp_path, config, readiness_timeout=10)
        assert contended.wait(timeout=5)
        with pytest.raises(TimeoutError):
            second.result(timeout=0.1)
        release.set()
        with pytest.raises(OSError, match="injected initial metadata"):
            first.result(timeout=10)
        result = second.result(timeout=20)
        assert result.generation is not None
        assert len(process_launches) == 1
        state = read_service_metadata(state_path)
        assert state is not None
        assert state["generation"] == result.generation
    finally:
        release.set()
        try:
            if not first.done():
                first.result(timeout=20)
            if second is not None and not second.done():
                second.result(timeout=20)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            stop_service(tmp_path, wait_timeout=10)
