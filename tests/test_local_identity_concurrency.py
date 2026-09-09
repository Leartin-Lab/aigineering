"""Concurrency coverage for local actor-key provisioning."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import multiprocessing
from pathlib import Path
from threading import Barrier

from aigineering.core.sqlite_store import SQLiteStore
import aigineering.local_identity as local_identity
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_plugin_publisher,
    ensure_local_runtime_publishers,
)


def _provision_runtime_publishers(db_path: str) -> tuple[tuple[str, str], ...]:
    store = SQLiteStore(db_path)
    try:
        registry = ensure_local_runtime_publishers(store)
        return tuple(
            (plugin_id, publisher.actor_key.public_key)
            for plugin_id, publisher in registry.publishers
        )
    finally:
        store.close()


def test_runtime_publishers_handle_forced_same_process_creation_race(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "fleet.db")
    bootstrap = SQLiteStore(db_path)
    try:
        ensure_local_domain(bootstrap)
    finally:
        bootstrap.close()

    creation_barrier = Barrier(2)
    original_write_actor_key = local_identity.write_actor_key

    def synchronized_write_actor_key(path: Path, signer) -> None:
        creation_barrier.wait(timeout=5)
        original_write_actor_key(path, signer)

    monkeypatch.setattr(local_identity, "write_actor_key", synchronized_write_actor_key)

    def provision_publishers(_slot: int) -> tuple[tuple[str, str], ...]:
        store = SQLiteStore(db_path)
        try:
            publisher = ensure_local_plugin_publisher(
                store, "planning.expand.v1", ("contract.publish",)
            )
            return tuple([("planning.expand.v1", publisher.actor_key.public_key)])
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(provision_publishers, range(4)))

    assert all(result == results[0] for result in results)


def test_runtime_publishers_are_safe_across_spawned_processes(tmp_path, monkeypatch):
    key_path = tmp_path / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    db_path = str(tmp_path / "spawned-fleet.db")
    bootstrap = SQLiteStore(db_path)
    try:
        ensure_local_domain(bootstrap)
    finally:
        bootstrap.close()

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        results = tuple(executor.map(_provision_runtime_publishers, (db_path, db_path)))

    assert results[0] == results[1]
    key_bytes = key_path.read_bytes()
    before_key_files = {
        path: path.read_bytes() for path in key_path.parent.glob("*.ed25519")
    }
    assert key_path.stat().st_mode & 0o077 == 0

    store = SQLiteStore(db_path)
    try:
        ensure_local_runtime_publishers(store)
    finally:
        store.close()

    assert key_bytes == key_path.read_bytes()
    key_files = tuple(key_path.parent.glob("*.ed25519"))
    assert key_files
    assert all(path.stat().st_mode & 0o077 == 0 for path in key_files)
    assert before_key_files == {path: path.read_bytes() for path in key_files}
    assert not tuple(key_path.parent.glob(".*.ed25519.*"))
