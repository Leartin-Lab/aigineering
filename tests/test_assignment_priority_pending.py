"""Accepted assignment and priority results survive required Redis relay delay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click import Group
from click.testing import CliRunner

from aigineering.cli.task_assignment import register_task_assignment_commands
from aigineering.cli.task_priority import register_task_priority_commands
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import ensure_local_domain
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_publication import publish_task_contract


def _cli(command: str) -> Group:
    root = Group()
    task = Group("task")
    if command == "assignment":
        register_task_assignment_commands(task)
    else:
        register_task_priority_commands(task)
    root.add_command(task)
    return root


def _workspace(tmp_path: Path, monkeypatch, command: str):
    import aigineering.application as application
    import aigineering.cli.task_assignment as cli_assignment
    import aigineering.cli.task_priority as cli_priority
    import aigineering.task_assignment as app_assignment
    import aigineering.task_priority as app_priority

    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    key_path = database.parent / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    monkeypatch.setattr(application, "require_redis", lambda: None)

    class _Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        application, "collaboration_service", lambda _store: _Collaboration()
    )
    store = SQLiteStore(database)
    ensure_local_domain(store)
    contract = build_control_plane_contract(
        name=f"{command} pending result",
        description="A focused post-commit relay-pending fixture.",
        outputs=("result",),
        budget=3,
    )
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )
    publish_task_contract(store, contract)
    if command == "assignment":
        store.register_worker(WorkerRegistration("worker:pending-one", capacity=2))
        store.register_worker(WorkerRegistration("worker:pending-two", capacity=2))
    store.close()

    if command == "assignment":
        monkeypatch.setattr(cli_assignment, "require_redis", lambda: None)
        monkeypatch.setattr(
            cli_assignment, "collaboration_service", lambda _store: _Collaboration()
        )
        monkeypatch.setattr(app_assignment, "require_redis", lambda: None)
        monkeypatch.setattr(
            app_assignment, "collaboration_service", lambda _store: _Collaboration()
        )
        monkeypatch.setattr(app_assignment, "sync_runtime", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            cli_assignment,
            "_persistent_snapshot",
            lambda: SQLiteStore.snapshot_from_path(database),
        )
        monkeypatch.setattr(
            cli_assignment, "_persistent_store", lambda: SQLiteStore(database)
        )
    else:
        monkeypatch.setattr(cli_priority, "require_redis", lambda: None)
        monkeypatch.setattr(
            cli_priority, "collaboration_service", lambda _store: _Collaboration()
        )
        monkeypatch.setattr(app_priority, "require_redis", lambda: None)
        monkeypatch.setattr(
            app_priority, "collaboration_service", lambda _store: _Collaboration()
        )
        monkeypatch.setattr(app_priority, "sync_runtime", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            cli_priority,
            "_persistent_snapshot",
            lambda: SQLiteStore.snapshot_from_path(database),
        )
        monkeypatch.setattr(
            cli_priority, "_persistent_store", lambda: SQLiteStore(database)
        )
    return database, contract


def _invoke(runner: CliRunner, command: str, args: list[str]):
    result = runner.invoke(_cli(command), ["task", *args])
    return result, json.loads(result.output)


@pytest.mark.parametrize(
    ("operation", "command"),
    [("assign", "assignment"), ("reassign", "assignment"), ("priority", "priority")],
)
@pytest.mark.parametrize("explicit_key", [None, "fixed-pending-key"])
def test_accepted_postcommit_pending_returns_exact_result_and_replays(
    tmp_path, monkeypatch, operation: str, command: str, explicit_key: str | None
):
    import aigineering.task_assignment as app_assignment
    import aigineering.task_priority as app_priority

    database, contract = _workspace(tmp_path, monkeypatch, command)
    runner = CliRunner()

    if operation == "assign":
        args = [
            "assign",
            contract.id,
            "worker:pending-one",
            "--reason",
            "relay recovery",
        ]
        record_type = "claim.assignment.changed"
        result_key = "assignment_id"
    elif operation == "reassign":
        seed, _seed_payload = _invoke(
            runner,
            command,
            [
                "assign",
                contract.id,
                "worker:pending-one",
                "--reason",
                "initial owner",
                "--idempotency-key",
                "initial-owner-key",
                "--json",
            ],
        )
        assert seed.exit_code == 0, f"{seed.output}\n{seed.exception!r}"
        seed_payload = json.loads(seed.output)
        previous_id = seed_payload["data"]["record_id"]
        args = [
            "reassign",
            contract.id,
            "--worker",
            "worker:pending-two",
            "--previous",
            previous_id,
            "--reason",
            "relay recovery",
        ]
        record_type = "claim.assignment.changed"
        result_key = "assignment_id"
    else:
        seed, seed_payload = _invoke(
            runner,
            command,
            [
                "priority",
                contract.id,
                "-5",
                "--reason",
                "initial priority",
                "--idempotency-key",
                "initial-priority-key",
                "--json",
            ],
        )
        assert seed.exit_code == 0, f"{seed.output}\n{seed.exception!r}"
        previous_priority_id = seed_payload["data"]["record_id"]
        args = [
            "priority",
            contract.id,
            "17",
            "--previous",
            previous_priority_id,
            "--reason",
            "relay recovery",
        ]
        record_type = "task.priority.changed"
        result_key = "record_id"
    if explicit_key is not None:
        args.extend(["--idempotency-key", explicit_key])
    args.append("--json")

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    owner = app_assignment if command == "assignment" else app_priority
    monkeypatch.setattr(owner, "sync_runtime", pending_after_commit)
    failed = runner.invoke(_cli(command), ["task", *args])
    assert failed.exit_code == 1, failed.output
    payload = json.loads(failed.output)
    assert payload["error"] == "redis_sync_pending", repr(failed.exception)
    assert payload["candidate_accepted"] is True
    assert payload["facts_retained"] is True
    data = payload["data"]
    assert isinstance(data["idempotency_key"], str) and data["idempotency_key"]
    if explicit_key is not None:
        assert data["idempotency_key"] == explicit_key
    assert data["candidate_id"].startswith("candidate:v1:")
    assert data["fact_ids"] == [data["record_id"]]
    assert data["record_id"] == data[result_key]
    assert type(payload["revision"]) is int

    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        direct = snapshot.scan_runtime_records(record_type=record_type)
        direct_ids = [record.id for _revision, record in direct]
        assert data["record_id"] in direct_ids
        assert len(direct_ids) == (2 if operation in {"reassign", "priority"} else 1)
        durable_revision = next(
            revision for revision, record in direct if record.id == data["record_id"]
        )
        assert durable_revision <= payload["revision"]
        assert (
            len(
                [
                    record
                    for _revision, record in snapshot.scan_runtime_records(
                        record_type="candidate.received"
                    )
                    if record.payload.get("candidate_id") == data["candidate_id"]
                ]
            )
            == 1
        )
    finally:
        snapshot.close()

    monkeypatch.setattr(owner, "sync_runtime", lambda *_a, **_kw: None)
    replay_args = list(args)
    if explicit_key is None:
        replay_args[-1:-1] = ["--idempotency-key", data["idempotency_key"]]
    replay, replay_payload = _invoke(runner, command, replay_args)
    assert replay.exit_code == 0, replay.output
    replay_data = replay_payload["data"]
    assert replay_data["idempotency_key"] == data["idempotency_key"]
    assert replay_data["candidate_id"] == data["candidate_id"]
    assert replay_data["fact_ids"] == data["fact_ids"]
    assert replay_data["record_id"] == data["record_id"]
    if operation == "reassign":
        assert replay_data["previous_assignment_id"] == previous_id
        assert data["previous_assignment_id"] == previous_id
    if operation == "priority":
        assert replay_data["previous_priority_id"] == previous_priority_id
        assert data["previous_priority_id"] == previous_priority_id

    changed_args = list(replay_args)
    changed_args[changed_args.index("relay recovery")] = "changed intent"
    changed, changed_payload = _invoke(runner, command, changed_args)
    assert changed.exit_code == 1, changed.output
    assert changed_payload["ok"] is False
    assert changed_payload["error"].get("code") != "redis_sync_pending"

    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert len(snapshot.scan_runtime_records(record_type=record_type)) == (
            2 if operation in {"reassign", "priority"} else 1
        )
    finally:
        snapshot.close()


@pytest.mark.parametrize("command", ["assignment", "priority"])
def test_prepublication_redis_failure_has_no_operation_result(
    tmp_path, monkeypatch, command: str
):
    import aigineering.task_assignment as app_assignment
    import aigineering.task_priority as app_priority

    database, contract = _workspace(tmp_path, monkeypatch, command)
    owner = app_assignment if command == "assignment" else app_priority

    def unavailable_before_publish(_store, *, accepted=None):
        if accepted is None:
            raise RedisSyncPending(accepted=accepted)

    monkeypatch.setattr(owner, "sync_runtime", unavailable_before_publish)
    if command == "assignment":
        args = ["assign", contract.id, "worker:pending-one", "--reason", "preflight"]
        record_type = "claim.assignment.changed"
    else:
        args = ["priority", contract.id, "17", "--reason", "preflight"]
        record_type = "task.priority.changed"
    result = CliRunner().invoke(_cli(command), ["task", *args, "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "redis_unavailable"
    assert payload["candidate_accepted"] is None
    assert "data" not in payload
    assert "revision" not in payload
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert snapshot.scan_runtime_records(record_type=record_type) == []
    finally:
        snapshot.close()


@pytest.mark.parametrize("command", ["assignment", "priority"])
def test_unexpected_postcommit_error_is_safe_internal_error(
    tmp_path, monkeypatch, command: str
):
    import aigineering.task_assignment as app_assignment
    import aigineering.task_priority as app_priority

    database, contract = _workspace(tmp_path, monkeypatch, command)
    owner = app_assignment if command == "assignment" else app_priority
    marker = "private-error-marker"

    def fail_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise TypeError(marker)

    monkeypatch.setattr(owner, "sync_runtime", fail_after_commit)
    if command == "assignment":
        args = ["assign", contract.id, "worker:pending-one", "--reason", "unexpected"]
        record_type = "claim.assignment.changed"
    else:
        args = ["priority", contract.id, "17", "--reason", "unexpected"]
        record_type = "task.priority.changed"
    result = CliRunner().invoke(_cli(command), ["task", *args, "--json"])
    assert result.exit_code == 1, result.output
    assert marker not in result.output
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["message"] == "Unexpected internal task command error."
    if command == "assignment":
        from aigineering.cli.task_assignment import _internal_error
    else:
        from aigineering.cli.task_priority import _internal_error
    from click.exceptions import ClickException

    cause = TypeError(marker)
    with pytest.raises(ClickException) as raised:
        _internal_error(cause, False)
    assert raised.value.__cause__ is cause
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert len(snapshot.scan_runtime_records(record_type=record_type)) == 1
    finally:
        snapshot.close()


@pytest.mark.parametrize("command", ["assignment", "priority"])
def test_rejected_decision_relay_pending_has_only_exact_rejection_ids(
    tmp_path, monkeypatch, command: str
):
    import aigineering.task_assignment as app_assignment
    import aigineering.task_priority as app_priority

    database, contract = _workspace(tmp_path, monkeypatch, command)
    owner = app_assignment if command == "assignment" else app_priority
    real_factory = owner._publisher
    interleaved = False

    class _InterleavingPublisher:
        def __init__(self, store):
            self.delegate = real_factory(store)

        def publish(self, *args, **kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                competing_store = SQLiteStore(database)
                try:
                    if command == "assignment":
                        app_assignment.publish_task_assignment(
                            competing_store,
                            contract.id,
                            "worker:pending-two",
                            "concurrent winner",
                            idempotency_key="concurrent-assignment-winner",
                        )
                    else:
                        app_priority.publish_task_priority(
                            competing_store,
                            contract.id,
                            -5,
                            "concurrent winner",
                            idempotency_key="concurrent-priority-winner",
                        )
                finally:
                    competing_store.close()
            return self.delegate.publish(*args, **kwargs)

    def publisher_factory(store):
        if interleaved:
            return real_factory(store)
        return _InterleavingPublisher(store)

    def pending_for_rejection(_store, *, accepted=None):
        if accepted is False:
            raise RedisSyncPending(accepted=False)

    monkeypatch.setattr(owner, "_publisher", publisher_factory)
    monkeypatch.setattr(owner, "sync_runtime", pending_for_rejection)
    if command == "assignment":
        args = [
            "assign",
            contract.id,
            "worker:pending-one",
            "--reason",
            "losing concurrent assignment",
        ]
    else:
        args = [
            "priority",
            contract.id,
            "17",
            "--reason",
            "losing concurrent priority",
        ]
    result = CliRunner().invoke(_cli(command), ["task", *args, "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "redis_sync_pending"
    assert payload["candidate_accepted"] is False
    assert payload["facts_retained"] is True
    assert payload["data"]["fact_ids"] == [payload["data"]["rejection_fact_id"]]
    assert "record_id" not in payload["data"]
    if command == "assignment":
        assert "idempotency_key" not in payload["data"]
    else:
        assert payload["data"]["idempotency_key"].startswith("task-priority:")
    assert "candidate_id" not in payload["data"]
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        direct_type = (
            "claim.assignment.changed"
            if command == "assignment"
            else "task.priority.changed"
        )
        direct = snapshot.scan_runtime_records(record_type=direct_type)
        assert len(direct) == 1
        assert direct[0][1].payload["reason"] == "concurrent winner"
        rejections = snapshot.scan_runtime_records(record_type="candidate.rejected")
        assert len(rejections) == 1
        assert payload["data"]["rejection_fact_id"] == rejections[0][1].id
        assert payload["data"]["fact_ids"] == [rejections[0][1].id]
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    ("operation", "command"),
    [("assign", "assignment"), ("reassign", "assignment"), ("priority", "priority")],
)
@pytest.mark.parametrize("construction", ["snapshot", "store"])
@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
@pytest.mark.parametrize("as_json", [True, False])
def test_store_construction_errors_are_safe_internal_errors(
    tmp_path,
    monkeypatch,
    operation: str,
    command: str,
    construction: str,
    error_type: type[Exception],
    as_json: bool,
):
    import aigineering.cli.task_assignment as cli_assignment
    import aigineering.cli.task_priority as cli_priority

    _database, contract = _workspace(tmp_path, monkeypatch, command)
    module = cli_assignment if command == "assignment" else cli_priority
    runner = CliRunner()

    if operation == "reassign":
        seed, seed_payload = _invoke(
            runner,
            command,
            [
                "assign",
                contract.id,
                "worker:pending-one",
                "--reason",
                "initial owner",
                "--idempotency-key",
                "initial-owner-key",
                "--json",
            ],
        )
        assert seed.exit_code == 0, seed.output
        args = [
            "reassign",
            contract.id,
            "--worker",
            "worker:pending-two",
            "--previous",
            seed_payload["data"]["record_id"],
            "--reason",
            "construction failure",
        ]
    elif operation == "assign":
        args = [
            "assign",
            contract.id,
            "worker:pending-one",
            "--reason",
            "construction failure",
        ]
    else:
        args = ["priority", contract.id, "17", "--reason", "construction failure"]
    if as_json:
        args.append("--json")

    marker = "private-construction-marker"
    original_internal_error = module._internal_error
    seen: list[Exception] = []

    def record_internal_error(exc: Exception, json_mode: bool) -> None:
        seen.append(exc)
        original_internal_error(exc, json_mode)

    monkeypatch.setattr(module, "_internal_error", record_internal_error)
    function_name = (
        "_persistent_snapshot" if construction == "snapshot" else "_persistent_store"
    )

    def fail_construction():
        raise error_type(marker)

    monkeypatch.setattr(module, function_name, fail_construction)
    result = runner.invoke(_cli(command), ["task", *args])

    assert result.exit_code == 1, result.output
    assert marker not in result.output
    assert len(seen) == 1 and isinstance(seen[0], error_type)
    if as_json:
        payload = json.loads(result.output)
        assert payload == {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": "Unexpected internal task command error.",
            },
        }
    else:
        assert "Unexpected internal task command error." in result.output


@pytest.mark.parametrize("command", ["assignment", "priority"])
@pytest.mark.parametrize("construction", ["snapshot", "store"])
def test_missing_workspace_during_construction_keeps_existing_error(
    tmp_path, monkeypatch, command: str, construction: str
):
    import aigineering.cli.task_assignment as cli_assignment
    import aigineering.cli.task_priority as cli_priority

    _database, contract = _workspace(tmp_path, monkeypatch, command)
    module = cli_assignment if command == "assignment" else cli_priority
    if command == "assignment":
        args = [
            "assign",
            contract.id,
            "worker:pending-one",
            "--reason",
            "missing store",
            "--json",
        ]
    else:
        args = ["priority", contract.id, "17", "--reason", "missing store", "--json"]

    if construction == "snapshot":
        monkeypatch.setattr(
            module,
            "_persistent_snapshot",
            lambda: (_ for _ in ()).throw(FileNotFoundError()),
        )
    else:
        monkeypatch.setattr(
            module,
            "_persistent_store",
            lambda: (_ for _ in ()).throw(FileNotFoundError()),
        )

    result = CliRunner().invoke(_cli(command), ["task", *args])
    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "ok": False,
        "error": {
            "code": "workspace_not_initialized",
            "message": "Initialize this workspace first.",
        },
    }


@pytest.mark.parametrize(
    ("command", "operation"),
    [("assignment", "assign"), ("priority", "priority")],
)
@pytest.mark.parametrize(
    "explicit_key",
    [None, "safe-key:123", "private key\nmarker", "private\x1b[31mkey"],
)
@pytest.mark.parametrize("as_json", [True, False])
def test_pending_human_replay_hint_never_echoes_unsafe_key(
    tmp_path,
    monkeypatch,
    command: str,
    operation: str,
    explicit_key: str | None,
    as_json: bool,
):
    import aigineering.task_assignment as app_assignment
    import aigineering.task_priority as app_priority

    _database, contract = _workspace(tmp_path, monkeypatch, command)
    owner = app_assignment if command == "assignment" else app_priority

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(owner, "sync_runtime", pending_after_commit)
    if operation == "assign":
        args = [
            "assign",
            contract.id,
            "worker:pending-one",
            "--reason",
            "safe replay hint",
        ]
    else:
        args = ["priority", contract.id, "17", "--reason", "safe replay hint"]
    if explicit_key is not None:
        args.extend(["--idempotency-key", explicit_key])
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(_cli(command), ["task", *args])

    assert result.exit_code == 1, result.output
    if as_json:
        payload = json.loads(result.output)
        assert payload["error"] == "redis_sync_pending"
        if explicit_key is not None:
            assert payload["data"]["idempotency_key"] == explicit_key
        else:
            prefix = "task-assignment:" if command == "assignment" else "task-priority:"
            assert payload["data"]["idempotency_key"].startswith(prefix)
    else:
        assert "Redis synchronization is pending; committed fact " in result.output
        if explicit_key is None:
            assert "Replay with --idempotency-key task-" in result.output
        elif explicit_key == "safe-key:123":
            assert "Replay with --idempotency-key safe-key:123." in result.output
        else:
            assert explicit_key not in result.output
            assert "Replay with the original --idempotency-key value;" in result.output
            assert "use --json to retrieve the exact key." in result.output


def test_priority_writer_rejection_preserves_exact_durable_reason(
    tmp_path, monkeypatch
):
    import aigineering.task_priority as app_priority

    database, contract = _workspace(tmp_path, monkeypatch, "priority")
    real_factory = app_priority._publisher
    interleaved = False

    class _InterleavingPublisher:
        def __init__(self, store):
            self.delegate = real_factory(store)

        def publish(self, *args, **kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                competing_store = SQLiteStore(database)
                try:
                    app_priority.publish_task_priority(
                        competing_store,
                        contract.id,
                        -5,
                        "concurrent winner",
                        idempotency_key="concurrent-priority-winner",
                    )
                finally:
                    competing_store.close()
            return self.delegate.publish(*args, **kwargs)

    monkeypatch.setattr(
        app_priority,
        "_publisher",
        lambda store: (
            _InterleavingPublisher(store) if not interleaved else real_factory(store)
        ),
    )
    result, payload = _invoke(
        CliRunner(),
        "priority",
        [
            "priority",
            contract.id,
            "17",
            "--reason",
            "losing concurrent priority",
            "--idempotency-key",
            "losing-priority-key",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    assert payload["error"]["code"] == "rejected"
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        durable = snapshot.scan_runtime_records(record_type="candidate.rejected")
        assert durable
        reason = durable[-1][1].payload["reason"]
        assert payload["error"]["message"] == reason
        assert payload["error"]["message"] == (
            "previous_priority_id does not match the exact current head"
        )
    finally:
        snapshot.close()
