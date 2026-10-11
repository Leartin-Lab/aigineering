"""Signed direct-Contract assignment behavior through the task CLI."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner

from aigineering.cli.task_assignment import register_task_assignment_commands
from aigineering.cli.task_handoff import register_task_handoff_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.protocol.effect_builders import contract_declaration_effect


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_assignment_commands(task)
    root.add_command(task)
    return root


def _install_store(monkeypatch, database: Path):
    import aigineering.application
    import aigineering.cli.task_assignment as cli_assignment
    import aigineering.task_assignment as app_assignment

    class _Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr(aigineering.application, "require_redis", lambda: None)
    monkeypatch.setattr(
        aigineering.application,
        "collaboration_service",
        lambda _store: _Collaboration(),
    )
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


def _workspace(tmp_path: Path, monkeypatch):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key = database.parent / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    root_signer = load_actor_signer(root_key)
    contract = build_control_plane_contract(
        name="assignment target",
        description="An assignment test target.",
        outputs=("result",),
        budget=3,
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        genesis.root_keys[0],
        root_signer,
        contract_declaration_effect(contract),
        idempotency_key="assignment-test-contract",
    )
    assert declared.accepted
    store.register_worker(WorkerRegistration("worker:one", capacity=2))
    store.register_worker(WorkerRegistration("worker:two", capacity=2))
    store.close()
    _install_store(monkeypatch, database)
    return database, root_key, contract


def _invoke(runner: CliRunner, args: list[str]) -> dict:
    result = runner.invoke(_app(), args)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["ok"] is True
    return parsed


def test_assign_reassign_clear_and_history_use_exact_signed_heads(
    tmp_path, monkeypatch
):
    database, _root_key, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    first_args = [
        "task",
        "assign",
        contract.id,
        "worker:one",
        "--reason",
        "initial owner",
        "--idempotency-key",
        "assignment-first",
        "--json",
    ]
    first = _invoke(runner, first_args)["data"]
    replay = _invoke(runner, first_args)["data"]
    assert first["record_id"] == replay["record_id"] == first["assignment_id"]
    assert first["record_type"] == "claim.assignment.changed"
    assert first["previous_assignment_id"] == ""
    assert first["worker_id"] == "worker:one"

    second_args = [
        "task",
        "reassign",
        contract.id,
        "--worker",
        "worker:two",
        "--previous",
        first["record_id"],
        "--reason",
        "rotation",
        "--idempotency-key",
        "assignment-rotation",
        "--json",
    ]
    second = _invoke(runner, second_args)["data"]
    second_replay = _invoke(runner, second_args)["data"]
    assert second_replay["record_id"] == second["record_id"]
    assert second["previous_assignment_id"] == first["record_id"]
    assert second["worker_id"] == "worker:two"

    clear_args = [
        "task",
        "reassign",
        contract.id,
        "--clear",
        "--previous",
        second["record_id"],
        "--reason",
        "release owner",
        "--idempotency-key",
        "assignment-clear",
        "--json",
    ]
    cleared = _invoke(runner, clear_args)["data"]
    clear_replay = _invoke(runner, clear_args)["data"]
    assert clear_replay["record_id"] == cleared["record_id"]
    assert cleared["previous_assignment_id"] == second["record_id"]
    assert cleared["worker_id"] == ""

    current = _invoke(runner, ["task", "assignments", contract.id, "--json"])
    assert current["data"]["head_assignment_id"] == cleared["record_id"]
    assert current["data"]["worker_id"] is None
    assert [row["record_id"] for row in current["data"]["history"]] == [
        first["record_id"],
        second["record_id"],
        cleared["record_id"],
    ]
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert snapshot.get_contract(contract.id) == contract
    finally:
        snapshot.close()


def test_invalid_target_does_not_provision_assignment_operator(tmp_path, monkeypatch):
    database, _root_key, _contract_value = _workspace(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            "missing-contract",
            "worker:one",
            "--reason",
            "invalid",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "not_found"
    bad_worker = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            _contract_value.id,
            "worker:missing",
            "--reason",
            "unknown worker",
            "--json",
        ],
    )
    assert bad_worker.exit_code == 1
    assert json.loads(bad_worker.output)["error"]["code"] == "invalid_input"
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert not any(
            item.record_type == "actor.authorized"
            and item.payload.get("actor_id") == "plugin:task.assignment.operator.v1"
            for _revision, item in snapshot.scan_runtime_records()
        )
    finally:
        snapshot.close()


def test_reassign_help_requires_existing_head_including_clear_head():
    result = CliRunner().invoke(_app(), ["task", "reassign", "--help"])
    assert result.exit_code == 0, result.output
    help_text = " ".join(result.output.split())
    assert "Exact current assignment head ID" in help_text
    assert "including a clear assignment" in help_text
    assert "empty string only when no assignment fact exists" not in help_text


def test_assignment_human_output_is_copyable_and_failure_is_click_error(
    tmp_path, monkeypatch
):
    database, _root_key, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            contract.id,
            "worker:one",
            "--reason",
            "initial owner",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Assignment record:" in result.output
    assert "\n  Contract:" in result.output
    assignment_id = result.output.split("Assignment ", 1)[1].splitlines()[0]
    assert assignment_id.startswith("record:")
    assert "Worker: worker:one" in result.output

    bad = runner.invoke(
        _app(), ["task", "assign", "missing-contract", "worker:one", "--reason", "x"]
    )
    assert bad.exit_code == 1
    assert "unknown Contract" in bad.output
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assignments = tuple(
            record
            for _revision, record in snapshot.scan_runtime_records(
                record_type="claim.assignment.changed"
            )
        )
        assert len(assignments) == 1
        assert assignments[0].id == assignment_id
    finally:
        snapshot.close()


def test_handoff_inherits_exact_assignment_and_signed_override_can_clear(
    tmp_path, monkeypatch
):
    database, root_key, source = _workspace(tmp_path, monkeypatch)
    import aigineering.cli.task_handoff as cli_handoff
    import aigineering.task_handoff as app_handoff

    monkeypatch.setattr(
        cli_handoff,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(cli_handoff, "_persistent_store", lambda: SQLiteStore(database))
    monkeypatch.setattr(cli_handoff, "require_redis", lambda: None)
    monkeypatch.setattr(app_handoff, "require_redis", lambda: None)
    monkeypatch.setattr(app_handoff, "sync_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        cli_handoff,
        "collaboration_service",
        lambda _s: type("Sync", (), {"sync": lambda self: None})(),
    )
    monkeypatch.setattr(
        app_handoff,
        "collaboration_service",
        lambda _s: type("Sync", (), {"sync": lambda self: None})(),
    )

    app = Group()
    task = Group("task")
    register_task_assignment_commands(task)
    register_task_handoff_commands(task)
    app.add_command(task)
    runner = CliRunner()

    def invoke_handoff(args):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        value = json.loads(result.output)
        assert value["ok"]
        return value["data"]

    assigned = invoke_handoff(
        [
            "task",
            "assign",
            source.id,
            "worker:one",
            "--reason",
            "root owner",
            "--idempotency-key",
            "root-assignment",
            "--json",
        ],
    )
    inherited = invoke_handoff(
        ["task", "handoff", source.id, "--reason", "continue root", "--json"],
    )
    assert inherited["assignment_mode"] == "inherit"
    assert inherited["source_assignment_id"] == assigned["record_id"]
    assert inherited["worker_id"] == "worker:one"
    assert inherited["assignment_fact_id"]

    # A second fresh workspace exercises explicit clear and its separately
    # provisioned authority without changing the fixed default handoff actor.
    database2, _root_key2, source2 = _workspace(tmp_path / "clear", monkeypatch)
    monkeypatch.setattr(
        cli_handoff,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database2),
    )
    monkeypatch.setattr(
        cli_handoff, "_persistent_store", lambda: SQLiteStore(database2)
    )
    assigned2 = invoke_handoff(
        [
            "task",
            "assign",
            source2.id,
            "worker:one",
            "--reason",
            "root owner",
            "--idempotency-key",
            "root-assignment",
            "--json",
        ],
    )
    cleared = invoke_handoff(
        [
            "task",
            "handoff",
            source2.id,
            "--reason",
            "clear successor owner",
            "--clear-assignment",
            "--source-assignment-id",
            assigned2["record_id"],
            "--json",
        ],
    )
    assert cleared["assignment_mode"] == "override"
    assert cleared["source_assignment_id"] == assigned2["record_id"]
    assert cleared["worker_id"] == ""
    assert cleared["assignment_fact_id"]
    snapshot = SQLiteStore.snapshot_from_path(database2)
    try:
        authorizations = tuple(
            record
            for _rev, record in snapshot.scan_runtime_records(
                record_type="actor.authorized"
            )
            if record.payload.get("actor_id")
            == "plugin:task.handoff.assignment-override.operator.v1"
        )
        assert authorizations
    finally:
        snapshot.close()


def test_handoff_override_can_set_worker_without_source_assignment(
    tmp_path, monkeypatch
):
    database, _root_key, source = _workspace(tmp_path, monkeypatch)
    import aigineering.cli.task_handoff as cli_handoff
    import aigineering.task_handoff as app_handoff

    monkeypatch.setattr(
        cli_handoff,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(cli_handoff, "_persistent_store", lambda: SQLiteStore(database))
    monkeypatch.setattr(cli_handoff, "require_redis", lambda: None)
    monkeypatch.setattr(app_handoff, "require_redis", lambda: None)
    monkeypatch.setattr(app_handoff, "sync_runtime", lambda *_a, **_kw: None)

    class _NoopCollaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        cli_handoff, "collaboration_service", lambda _s: _NoopCollaboration()
    )
    monkeypatch.setattr(
        app_handoff, "collaboration_service", lambda _s: _NoopCollaboration()
    )
    app = Group()
    task = Group("task")
    register_task_assignment_commands(task)
    register_task_handoff_commands(task)
    app.add_command(task)
    runner = CliRunner()

    denied = runner.invoke(
        app,
        [
            "task",
            "handoff",
            source.id,
            "--reason",
            "reject unknown worker",
            "--assignment-worker",
            "worker:missing",
            "--source-assignment-id",
            "",
            "--json",
        ],
    )
    assert denied.exit_code == 1
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert not any(
            record.record_type == "actor.authorized"
            and record.payload.get("actor_id")
            == "plugin:task.handoff.assignment-override.operator.v1"
            for _revision, record in snapshot.scan_runtime_records()
        )
    finally:
        snapshot.close()

    result = runner.invoke(
        app,
        [
            "task",
            "handoff",
            source.id,
            "--reason",
            "explicitly route successor",
            "--assignment-worker",
            "worker:two",
            "--source-assignment-id",
            "",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["assignment_mode"] == "override"
    assert payload["source_assignment_id"] == ""
    assert payload["worker_id"] == "worker:two"
    assert payload["assignment_fact_id"]
