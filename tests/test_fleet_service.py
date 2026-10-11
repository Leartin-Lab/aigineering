from __future__ import annotations

import json
import hashlib
import os
import stat
import threading
import fcntl
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.fleet_service import (
    FleetServiceError,
    PullAdmissionGate,
    canonical_service_store_path,
    read_service_metadata,
    status_service,
    stop_service,
    write_service_metadata,
    start_service,
    _open_lock,
    _service_dir,
)
from aigineering.cli.main import cli
import aigineering.fleet_service as fleet_service_module


def _enable_fixture_factory_import(monkeypatch):
    """Expose this test tree to both adapter preflight and its child process."""
    test_root = str(Path(__file__).resolve().parent)
    monkeypatch.syspath_prepend(test_root)
    existing = os.environ.get("PYTHONPATH", "")
    paths = [item for item in existing.split(os.pathsep) if item and item != test_root]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([test_root, *paths]))


def test_service_requires_the_selected_workspace_default_store(tmp_path):
    expected = (tmp_path / ".aig" / "store.db").resolve()
    assert canonical_service_store_path(tmp_path, ".aig/store.db") == expected
    assert canonical_service_store_path(tmp_path, "./.aig/../.aig/store.db") == expected

    with pytest.raises(FleetServiceError, match="selected workspace Store"):
        canonical_service_store_path(tmp_path, "other.db")
    with pytest.raises(FleetServiceError, match="selected workspace Store"):
        canonical_service_store_path(tmp_path, ":memory:")


def test_start_rejects_custom_store_before_service_setup(tmp_path):
    config = tmp_path / "fleet.toml"
    config.write_text(
        '[fleet]\ndb_path="other.db"\n'
        '[[workers]]\nid="local"\nkind="local"\nworker_factory="unused:factory"\n',
        encoding="utf-8",
    )
    with pytest.raises(FleetServiceError, match="selected workspace Store"):
        start_service(tmp_path, config)
    assert not (tmp_path / ".aig" / "fleet-service").exists()


def test_start_requires_existing_default_store_before_registration(tmp_path):
    config = tmp_path / "fleet.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="local"\nkind="local"\nworker_factory="unused:factory"\n',
        encoding="utf-8",
    )
    with pytest.raises(FleetServiceError, match="Workspace Store is missing"):
        start_service(tmp_path, config)
    assert not (tmp_path / ".aig" / "fleet-service").exists()


def test_redis_preflight_failure_writes_no_registration_then_recovers(
    tmp_path, monkeypatch
):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL for actual Fleet Redis recovery acceptance")
    from aigineering.adapters.redis_query import QueryProjectionUnavailable
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain

    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    monkeypatch.setenv("AIGINEERING_REDIS_URL", redis_url)
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="service-worker"\nkind="local"\n'
        'worker_factory="fixtures.fleet_service_worker:build_worker"\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)
        original_ids = {record.id for _, record in store.scan_runtime_records()}

    real_require_redis = fleet_service_module.require_redis

    def unavailable():
        raise QueryProjectionUnavailable("controlled preflight fault")

    monkeypatch.setattr(fleet_service_module, "require_redis", unavailable)
    with pytest.raises(FleetServiceError) as exc_info:
        start_service(tmp_path, config)
    assert exc_info.value.code == "redis_unavailable"
    assert not (tmp_path / ".aig" / "fleet-service").exists()
    with SQLiteStore(".aig/store.db") as store:
        assert {record.id for _, record in store.scan_runtime_records()} == original_ids

    monkeypatch.setattr(fleet_service_module, "require_redis", real_require_redis)
    recovered = start_service(tmp_path, config, readiness_timeout=15)
    assert recovered.state == "ready"
    assert stop_service(tmp_path, wait_timeout=10).state == "stopped"


def test_start_rejects_llm_without_configured_environment_credential_before_setup(
    tmp_path, monkeypatch
):
    from aigineering.core.sqlite_store import SQLiteStore

    monkeypatch.delenv("AIG_TEST_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("AIGINEERING_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with SQLiteStore(tmp_path / ".aig" / "store.db"):
        pass
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="missing-key-worker"\nkind="llm"\n'
        'model="example-model"\napi_key_env="AIG_TEST_PROVIDER_KEY"\n',
        encoding="utf-8",
    )

    with pytest.raises(FleetServiceError) as exc_info:
        start_service(tmp_path, config)

    assert exc_info.value.code == "worker_credentials_missing"
    assert "AIG_TEST_PROVIDER_KEY" not in str(exc_info.value)
    assert "missing-key-worker" not in str(exc_info.value)
    assert not (tmp_path / ".aig" / "fleet-service").exists()


@pytest.mark.parametrize("state_kind", ["malformed", "symlink"])
def test_start_rejects_corrupt_or_unsafe_state_before_launch(
    tmp_path, monkeypatch, state_kind
):
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain

    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    monkeypatch.setattr(fleet_service_module, "require_redis", lambda: None)
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="service-worker"\nkind="local"\n'
        'worker_factory="fixtures.fleet_service_worker:build_worker"\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)
        before_records = {record.id for _, record in store.scan_runtime_records()}
    service_dir = tmp_path / ".aig" / "fleet-service"
    service_dir.mkdir(mode=0o700)
    state_path = service_dir / "state.json"
    if state_kind == "malformed":
        original = b"{malformed json\n"
        state_path.write_bytes(original)
    else:
        target = tmp_path / "unsafe-state-target.json"
        target.write_text('{"schema":"aig-fleet-service-v1"}', encoding="utf-8")
        state_path.symlink_to(target)
        original = None
    launched = []

    def forbidden_popen(*args, **kwargs):
        launched.append((args, kwargs))
        raise AssertionError("unsafe state reached process launch")

    monkeypatch.setattr(fleet_service_module.subprocess, "Popen", forbidden_popen)
    with pytest.raises(FleetServiceError) as exc_info:
        start_service(tmp_path, config)

    assert exc_info.value.code == "unsafe_state"
    assert launched == []
    if original is not None:
        assert state_path.read_bytes() == original
    else:
        assert state_path.is_symlink()
    check = SQLiteStore(".aig/store.db")
    try:
        assert {
            record.id for _, record in check.scan_runtime_records()
        } == before_records
    finally:
        check.close()


@pytest.mark.parametrize("child_state", ["failed", "ready"])
def test_parent_does_not_overwrite_child_state_after_process_launch(
    tmp_path, monkeypatch, child_state
):
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain

    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    monkeypatch.setattr(fleet_service_module, "require_redis", lambda: None)
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="service-worker"\nkind="local"\n'
        'worker_factory="fixtures.fleet_service_worker:build_worker"\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)

    class ChildProcess:
        pid = 43210

        def poll(self):
            return None

    def child_already_updated_state(*args, **kwargs):
        state_path = Path(kwargs["env"]["AIG_FLEET_SERVICE_STATE"])
        state = read_service_metadata(state_path)
        assert state is not None
        state["state"] = child_state
        state["error_code"] = (
            "worker_adapter_invalid" if child_state == "failed" else None
        )
        if child_state == "ready":
            state["pid"] = 43210
            state["worker_ids"] = ["service-worker"]
            state["registration_versions"] = ["1"]
        write_service_metadata(state_path, state)
        return ChildProcess()

    monkeypatch.setattr(
        fleet_service_module.subprocess, "Popen", child_already_updated_state
    )
    if child_state == "failed":
        with pytest.raises(FleetServiceError) as exc_info:
            start_service(tmp_path, config, readiness_timeout=0.2)
        assert exc_info.value.code == "worker_adapter_invalid"
        assert status_service(tmp_path).error_code == "worker_adapter_invalid"
        assert status_service(tmp_path).state == "failed"
    else:
        result = start_service(tmp_path, config, readiness_timeout=0.2)
        assert result.state == "ready"
        assert result.worker_ids == ("service-worker",)


def test_adapters_are_constructed_before_process_launch(tmp_path, monkeypatch):
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain

    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    monkeypatch.setattr(fleet_service_module, "require_redis", lambda: None)
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="invalid-factory-worker"\nkind="local"\n'
        'worker_factory="fixtures.fleet_service_worker:missing_factory"\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)
    launched = []

    def forbidden_popen(*args, **kwargs):
        launched.append((args, kwargs))
        raise AssertionError("invalid adapter reached process launch")

    monkeypatch.setattr(fleet_service_module.subprocess, "Popen", forbidden_popen)
    with pytest.raises(FleetServiceError) as exc_info:
        start_service(tmp_path, config)
    assert exc_info.value.code == "worker_adapter_invalid"
    assert launched == []
    assert not (tmp_path / ".aig" / "fleet-service" / "state.json").exists()


def test_pull_gate_stops_new_admissions_but_preserves_admitted_call():
    gate = PullAdmissionGate()
    admission = gate.admit()
    assert admission is not None
    gate.close()
    assert gate.admit() is None
    assert gate.active == 1
    admission.release()
    assert gate.active == 0
    assert gate.drained


def test_service_metadata_is_private_and_contains_no_task_state(tmp_path):
    path = tmp_path / ".aig" / "fleet-service" / "state.json"
    state = {
        "schema": "aig-fleet-service-v1",
        "state": "starting",
        "generation": "a" * 32,
        "pid": 123,
        "workspace": str(tmp_path.resolve()),
        "domain_id": "domain:v1:" + "b" * 64,
        "config_path": str((tmp_path / "fleet.toml").resolve()),
        "config_sha256": "c" * 64,
        "worker_ids": [],
        "registration_versions": [],
        "safe_error": None,
    }
    write_service_metadata(path, state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    persisted = json.loads(path.read_text())
    assert persisted == state
    assert not any(
        key in persisted for key in ("contract_id", "task_status", "prompt", "api_key")
    )


def test_pull_gate_admit_and_close_are_linearized():
    gate = PullAdmissionGate()
    admitted = []
    barrier = threading.Barrier(2)

    def pull():
        barrier.wait()
        token = gate.admit()
        admitted.append(token is not None)
        if token is not None:
            token.release()

    thread = threading.Thread(target=pull)
    thread.start()
    barrier.wait()
    gate.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert admitted == [False]


def test_status_and_stop_without_service_are_read_only(tmp_path):
    assert status_service(tmp_path).state == "stopped"
    assert stop_service(tmp_path).state == "stopped"
    assert not (tmp_path / ".aig").exists()


@pytest.mark.parametrize("command", ["status", "stop"])
def test_local_controls_are_useful_offline_and_do_not_initialize_workspace(
    tmp_path, command
):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["--offline", "fleet", command, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload == {
            "ok": True,
            "data": {
                "state": "stopped",
                "generation": None,
                "domain_id": None,
                "active_slots": 0,
                "worker_ids": [],
                "registration_versions": [],
                "config_sha256": None,
                "config_drift": None,
                "error_code": None,
            },
        }
        assert not Path(".aig").exists()


def test_corrupt_service_metadata_fails_closed(tmp_path):
    service_dir = _service_dir(tmp_path)
    path = service_dir / "state.json"
    write_service_metadata(
        path,
        {
            "schema": "aig-fleet-service-v1",
            "state": "ready",
            "generation": "z" * 32,
            "pid": 123,
            "workspace": str(tmp_path),
            "domain_id": None,
            "config_path": "x",
            "config_sha256": "x",
            "worker_ids": [],
            "registration_versions": [],
            "active_slots": True,
        },
    )
    with pytest.raises(FleetServiceError, match="metadata is invalid"):
        status_service(tmp_path)


def test_live_generation_stop_uses_metadata_request_not_pid_signal(tmp_path):
    service_dir = _service_dir(tmp_path)
    lock_fd = _open_lock(service_dir / "owner.lock")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    generation = "d" * 32
    state = {
        "schema": "aig-fleet-service-v1",
        "state": "ready",
        "generation": generation,
        "pid": 987654321,
        "workspace": str(tmp_path.resolve()),
        "domain_id": "domain:v1:" + "e" * 64,
        "config_path": str((tmp_path / "fleet.toml").resolve()),
        "config_sha256": "f" * 64,
        "worker_ids": [],
        "registration_versions": [],
        "active_slots": 0,
        "error_code": None,
    }
    write_service_metadata(service_dir / "state.json", state)
    try:
        assert status_service(tmp_path).state == "ready"
        result = stop_service(tmp_path, wait_timeout=0.02)
        assert result.state == "stopping"
        request = read_service_metadata(service_dir / "stop.json")
        assert request == {
            "action": "stop",
            "generation": generation,
            "schema": "aig-fleet-service-v1",
        }
        assert status_service(tmp_path).state == "ready"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_state_read_merge_write_serializes_ready_and_stopping(tmp_path, monkeypatch):
    service_dir = _service_dir(tmp_path)
    state_path = service_dir / "state.json"
    generation = "e" * 32
    initial = {
        "schema": "aig-fleet-service-v1",
        "state": "ready",
        "generation": generation,
        "active_slots": 0,
        "worker_ids": [],
        "registration_versions": [],
        "error_code": None,
    }
    write_service_metadata(state_path, initial)

    ready_writer_entered = threading.Event()
    release_ready_writer = threading.Event()
    stop_writer_started = threading.Event()
    original_write = fleet_service_module.write_service_metadata

    def pause_old_ready_write(path, payload):
        if (
            threading.current_thread().name == "old-ready-writer"
            and payload.get("state") == "ready"
        ):
            ready_writer_entered.set()
            assert release_ready_writer.wait(timeout=2)
        original_write(path, payload)

    monkeypatch.setattr(
        fleet_service_module, "write_service_metadata", pause_old_ready_write
    )

    ready_writer = threading.Thread(
        name="old-ready-writer",
        target=lambda: fleet_service_module._update_state(
            state_path, initial, state="ready", active_slots=1
        ),
    )

    def stop_writer():
        stop_writer_started.set()
        fleet_service_module._update_state(
            state_path, initial, state="stopping", active_slots=1
        )

    stopping_writer = threading.Thread(name="stopping-writer", target=stop_writer)
    ready_writer.start()
    assert ready_writer_entered.wait(timeout=2)
    # The lock covers the pending write as well as the preceding read/merge.
    lock_available = fleet_service_module._STATE_WRITE_LOCK.acquire(blocking=False)
    if lock_available:
        fleet_service_module._STATE_WRITE_LOCK.release()
    assert not lock_available
    stopping_writer.start()
    assert stop_writer_started.wait(timeout=2)
    release_ready_writer.set()
    ready_writer.join(timeout=2)
    stopping_writer.join(timeout=2)
    assert not ready_writer.is_alive()
    assert not stopping_writer.is_alive()

    state = read_service_metadata(state_path)
    assert state is not None
    assert state["state"] == "stopping"
    assert state["active_slots"] == 1


def test_late_ready_update_cannot_reopen_stopping_generation(tmp_path):
    service_dir = _service_dir(tmp_path)
    state_path = service_dir / "state.json"
    generation = "f" * 32
    initial = {
        "schema": "aig-fleet-service-v1",
        "state": "stopping",
        "generation": generation,
        "active_slots": 1,
        "worker_ids": [],
        "registration_versions": [],
        "error_code": None,
    }
    write_service_metadata(state_path, initial)

    fleet_service_module._update_state(
        state_path, initial, state="ready", active_slots=0
    )

    state = read_service_metadata(state_path)
    assert state is not None
    assert state["state"] == "stopping"
    # Late slot accounting remains observable without reopening admission.
    assert state["active_slots"] == 0


@pytest.mark.parametrize(
    ("terminal_state", "active_slots", "error_code"),
    [
        ("stopped", 0, None),
        ("failed", 1, "worker_failure"),
        ("degraded", 0, "degraded"),
    ],
)
def test_late_watcher_cannot_rewrite_terminal_service_snapshot(
    tmp_path, terminal_state, active_slots, error_code
):
    service_dir = _service_dir(tmp_path)
    state_path = service_dir / "state.json"
    generation = "a" * 32
    terminal = {
        "schema": "aig-fleet-service-v1",
        "state": terminal_state,
        "generation": generation,
        "active_slots": active_slots,
        "worker_ids": ["worker:fixture"],
        "registration_versions": ["registration-v1"],
        "error_code": error_code,
    }
    write_service_metadata(state_path, terminal)
    before = state_path.read_bytes()
    gate = PullAdmissionGate()

    class ImmediateStopEvent:
        was_set = False

        def wait(self, _timeout):
            return False

        def set(self):
            self.was_set = True

    stop_event = ImmediateStopEvent()
    fleet_service_module._watch_stop(
        lambda: True,
        gate,
        stop_event,
        state_path,
        {"generation": generation, "state": "ready"},
    )

    assert gate.closed
    assert stop_event.was_set
    assert state_path.read_bytes() == before


def test_persistent_service_claims_domain_tasks_and_drains_without_reset(
    tmp_path, monkeypatch
):
    import time

    from aigineering.core.control_plane import build_control_plane_contract
    from aigineering.core.runtime_projection import RuntimeProjection
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain
    from aigineering.local_publication import commit_local_effects, require_accepted
    from aigineering.protocol.effect_builders import contract_declaration_effect

    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="service-worker"\nkind="local"\ncapacity=2\n'
        'capabilities=["service.test"]\n'
        'worker_factory="fixtures.fleet_service_worker:build_worker"\n',
        encoding="utf-8",
    )
    store = SQLiteStore(".aig/store.db")
    ensure_local_domain(store)
    store.close()

    result = start_service(tmp_path, config, readiness_timeout=15)
    assert result.state == "ready"
    assert result.worker_ids == ("service-worker",)
    assert result.registration_versions

    store = SQLiteStore(".aig/store.db")
    contracts = tuple(
        build_control_plane_contract(
            name=f"service-task-{index}",
            outputs=(f"service_output_{index}",),
            budget=1,
            worker_capabilities=("service.test",),
        )
        for index in range(2)
    )
    unrelated = build_control_plane_contract(
        name="service-unrouted-task",
        outputs=("service_unrouted_output",),
        budget=1,
        worker_capabilities=("other.capability",),
    )
    require_accepted(
        commit_local_effects(
            store,
            tuple(
                contract_declaration_effect(item) for item in (*contracts, unrelated)
            ),
            idempotency_key="service-task-fixture",
        )
    )
    store.close()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        check = SQLiteStore(".aig/store.db")
        try:
            views = [
                RuntimeProjection(check, check).contract_view(item)
                for item in contracts
            ]
            if all(view.terminal == "complete" for view in views):
                break
        finally:
            check.close()
        time.sleep(0.05)
    else:
        stop_service(tmp_path, wait_timeout=5)
        pytest.fail("persistent Fleet did not complete both signed task outputs")

    observed = status_service(tmp_path)
    assert observed.state == "ready"
    after_status = SQLiteStore(".aig/store.db")
    try:
        ids_after_status = {
            record.id for _, record in after_status.scan_runtime_records()
        }
    finally:
        after_status.close()
    status_result = status_service(tmp_path)
    assert status_result.state == "ready"
    status_check = SQLiteStore(".aig/store.db")
    try:
        assert {
            record.id for _, record in status_check.scan_runtime_records()
        } == ids_after_status
    finally:
        status_check.close()

    stopped = stop_service(tmp_path, wait_timeout=10)
    assert stopped.state == "stopped"
    final = SQLiteStore(".aig/store.db")
    try:
        claims = [
            record
            for _, record in final.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id") in {item.id for item in contracts}
        ]
        assert len(claims) == 2
        assert {record.payload["contract_id"] for record in claims} == {
            item.id for item in contracts
        }
        assert not any(
            record.payload.get("contract_id") == unrelated.id
            for _, record in final.scan_runtime_records(record_type="claim.granted")
        )
        final_ids = {record.id for _, record in final.scan_runtime_records()}
        assert final_ids == ids_after_status
    finally:
        final.close()


def test_duplicate_start_and_restart_keep_exact_generation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain

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

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: start_service(tmp_path, config, readiness_timeout=15),
                range(2),
            )
        )
    generations = {item.generation for item in results}
    assert len(generations) == 1
    assert all(item.state in {"ready", "starting"} for item in results)
    state_path = tmp_path / ".aig" / "fleet-service" / "state.json"
    running_state = read_service_metadata(state_path)
    assert running_state is not None
    assert running_state["generation"] in generations

    changed_config = config.read_text(encoding="utf-8").replace("0.02", "0.03")
    config.write_text(changed_config, encoding="utf-8")
    drifted = status_service(tmp_path)
    assert drifted.config_drift is True
    repeated = start_service(tmp_path, config, readiness_timeout=15)
    assert repeated.generation == next(iter(generations))
    assert repeated.config_sha256 != hashlib.sha256(config.read_bytes()).hexdigest()

    stopped = stop_service(tmp_path, wait_timeout=10)
    assert stopped.state == "stopped"
    restarted = start_service(tmp_path, config, readiness_timeout=15)
    assert restarted.state == "ready"
    assert restarted.generation not in generations
    assert restarted.config_sha256 == hashlib.sha256(config.read_bytes()).hexdigest()
    assert stop_service(tmp_path, wait_timeout=10).state == "stopped"


def test_stop_gate_drains_admitted_invocation_and_blocks_later_claims(
    tmp_path, monkeypatch
):
    import time

    from aigineering.core.control_plane import build_control_plane_contract
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain
    from aigineering.local_publication import commit_local_effects, require_accepted
    from aigineering.protocol.effect_builders import contract_declaration_effect

    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    started = tmp_path / "worker.started"
    release = tmp_path / "worker.release"
    monkeypatch.setenv("AIG_FLEET_TEST_STARTED_FILE", str(started))
    monkeypatch.setenv("AIG_FLEET_TEST_RELEASE_FILE", str(release))
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="barrier-worker"\nkind="local"\ncapacity=1\n'
        'capabilities=["service.test"]\n'
        'worker_factory="fixtures.fleet_service_worker:build_barrier_worker"\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)
    start_service(tmp_path, config, readiness_timeout=15)
    tasks = tuple(
        build_control_plane_contract(
            name=f"stop-gate-task-{index}",
            outputs=(f"stop_gate_output_{index}",),
            budget=1,
            worker_capabilities=("service.test",),
        )
        for index in range(2)
    )
    with SQLiteStore(".aig/store.db") as store:
        require_accepted(
            commit_local_effects(
                store,
                tuple(contract_declaration_effect(task) for task in tasks),
                idempotency_key="stop-gate-task-fixture",
            )
        )
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists(), "Worker did not enter the admitted invocation"

    timed_out = stop_service(tmp_path, wait_timeout=0.05)
    assert timed_out.state == "stopping"
    state_path = tmp_path / ".aig" / "fleet-service" / "state.json"
    ack_deadline = time.monotonic() + 2
    live_state = None
    while time.monotonic() < ack_deadline:
        live_state = read_service_metadata(state_path)
        if live_state is not None and live_state["state"] == "stopping":
            break
        time.sleep(0.01)
    assert live_state is not None and live_state["state"] == "stopping"
    assert live_state["active_slots"] == 1
    with SQLiteStore(".aig/store.db") as store:
        admitted_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id") in {task.id for task in tasks}
        ]
    assert len(admitted_claims) == 1

    release.write_text("continue", encoding="utf-8")
    assert stop_service(tmp_path, wait_timeout=10).state == "stopped"
    with SQLiteStore(".aig/store.db") as store:
        claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id") in {task.id for task in tasks}
        ]
        assert len(claims) == 1
        assert store.get_contract(tasks[0].id) is not None
        assert store.get_contract(tasks[1].id) is not None


def test_process_crash_preserves_active_claim_across_service_restart(
    tmp_path, monkeypatch
):
    import signal
    import time

    from aigineering.core.control_plane import build_control_plane_contract
    from aigineering.core.runtime_projection import RuntimeProjection
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.local_identity import ensure_local_domain
    from aigineering.local_publication import commit_local_effects, require_accepted
    from aigineering.protocol.effect_builders import contract_declaration_effect

    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    started = tmp_path / "crash-worker.started"
    release = tmp_path / "crash-worker.release"
    monkeypatch.setenv("AIG_FLEET_TEST_STARTED_FILE", str(started))
    monkeypatch.setenv("AIG_FLEET_TEST_RELEASE_FILE", str(release))
    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="crash-barrier-worker"\nkind="local"\ncapacity=1\n'
        'capabilities=["service.test"]\n'
        'worker_factory="fixtures.fleet_service_worker:build_barrier_worker"\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)
    start_service(tmp_path, config, readiness_timeout=15)
    task = build_control_plane_contract(
        name="crash-preserves-active-claim",
        outputs=("crash_output",),
        budget=1,
        worker_capabilities=("service.test",),
    )
    with SQLiteStore(".aig/store.db") as store:
        require_accepted(
            commit_local_effects(
                store,
                (contract_declaration_effect(task),),
                idempotency_key="crash-active-claim-fixture",
            )
        )
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists(), "Worker did not enter the claimed invocation"
    crashed_state = read_service_metadata(
        tmp_path / ".aig" / "fleet-service" / "state.json"
    )
    assert crashed_state is not None
    assert crashed_state["state"] == "ready"
    pid = crashed_state["pid"]
    assert type(pid) is int and pid > 0

    # This is test-only termination of the exact child just launched, to model
    # process death during an admitted invocation. Production stop never signals
    # a PID and uses the generation lock/request protocol instead.
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if status_service(tmp_path).state == "stale":
            break
        time.sleep(0.02)
    else:
        pytest.fail("crashed service did not release its process lock")

    with SQLiteStore(".aig/store.db") as store:
        first_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id") == task.id
        ]
        assert len(first_claims) == 1
        first_claim_id = first_claims[0].id
        first_lease_id = store.get_claim(task.id)["claim_id"]
        view = RuntimeProjection(store, store).contract_view(task)
        assert view.current_claim_id == first_lease_id
        assert view.terminal is None
        assert not view.outputs_satisfied

    restarted = start_service(tmp_path, config, readiness_timeout=15)
    assert restarted.state == "ready"
    with SQLiteStore(".aig/store.db") as store:
        after_restart_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id") == task.id
        ]
        assert [record.id for record in after_restart_claims] == [first_claim_id]
        assert store.get_claim(task.id)["claim_id"] == first_lease_id
        view = RuntimeProjection(store, store).contract_view(task)
        assert view.current_claim_id == first_lease_id
        assert view.terminal is None
        assert not view.outputs_satisfied
    assert stop_service(tmp_path, wait_timeout=10).state == "stopped"
