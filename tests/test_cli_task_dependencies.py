"""Signed CLI behavior for exact task claim-dependency facts."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner

from aigineering.cli.task_dependencies import register_task_dependency_commands
from aigineering.core.ids import compute_content_hash, hash_contract
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.provenance import sign_asset
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.types import Asset, Contract
from aigineering.task_views import project_task_status


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_dependency_commands(task)
    root.add_command(task)
    return root


def _contract(name: str, *, outputs: tuple[str, ...] = ()) -> Contract:
    return Contract(
        id=hash_contract(
            name=name,
            description=f"{name} description",
            inputs=[],
            outputs=list(outputs),
            activation="",
            budget=3,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name=name,
        description=f"{name} description",
        outputs=outputs,
        budget=3,
    )


def _workspace(tmp_path: Path, monkeypatch, *, target_outputs: tuple[str, ...] = ()):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    store = SQLiteStore(database)
    ensure_local_domain(store)
    target = _contract("dependency target", outputs=target_outputs)
    prerequisite = _contract("dependency prerequisite")
    store.add_contract(target)
    store.add_contract(prerequisite)
    store.close()

    monkeypatch.setattr(
        "aigineering.cli.task_dependencies._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_dependencies._persistent_store",
        lambda: SQLiteStore(database),
    )
    monkeypatch.setattr("aigineering.cli.task_dependencies.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.task_dependencies.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.task_collaboration.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.application.require_redis", lambda: None)

    class _Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        "aigineering.application.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_dependencies.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.task_dependencies.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.task_dependencies.sync_runtime", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", lambda *_args, **_kwargs: None
    )
    return database, root_key_path, target, prerequisite


def _plugin_key_path(root_key_path: Path) -> Path:
    actor_id = "plugin:task.dependencies.operator.v1"
    suffix = compute_content_hash(actor_id)[:16]
    return root_key_path.parent / f"plugin-{suffix}.ed25519"


def _invoke(runner: CliRunner, args: list[str]) -> dict:
    result = runner.invoke(_app(), args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def test_link_and_unlink_create_exact_signed_history_and_are_replayable(
    tmp_path, monkeypatch
):
    database, root_key_path, target, prerequisite = _workspace(
        tmp_path, monkeypatch, target_outputs=("deliverable",)
    )
    runner = CliRunner()
    args = [
        "task",
        "link",
        target.id,
        "--depends-on",
        prerequisite.id,
        "--reason",
        "wait for source",
        "--idempotency-key",
        "dep-link-1",
        "--json",
    ]
    first = _invoke(runner, args)["data"]
    replay = _invoke(runner, args)["data"]
    assert first["record_id"] == replay["record_id"] == first["constraint_id"]
    assert first["record_type"] == "claim.constraint.placed"
    assert first["contract_id"] == target.id
    assert first["prerequisite_contract_id"] == prerequisite.id
    assert first["predicate"] == "prerequisite_terminal"
    assert first["required_terminal"] == "complete"
    assert first["actor_id"] == "plugin:task.dependencies.operator.v1"
    assert first["key_id"]
    assert first["candidate_id"]

    history = _invoke(runner, ["task", "dependencies", target.id, "--json"])
    assert history["data"][0]["constraint_id"] == first["constraint_id"]
    assert history["data"][0]["active"] is True
    status_store = SQLiteStore.snapshot_from_path(database)
    try:
        status = project_task_status(target, status_store)
    finally:
        status_store.close()
    assert any(
        item.startswith(f"prerequisite:{first['constraint_id']}:{prerequisite.id}:")
        for item in status["blockers"]
    )
    assert status["status"] == "blocked"
    assert status["dependencies"][0]["constraint_id"] == first["constraint_id"]

    release_args = [
        "task",
        "unlink",
        target.id,
        "--link",
        first["constraint_id"],
        "--reason",
        "source failed",
        "--idempotency-key",
        "dep-unlink-1",
        "--json",
    ]
    released = _invoke(runner, release_args)["data"]
    assert released["record_type"] == "claim.constraint.released"
    assert released["constraint_id"] == first["constraint_id"]
    release_store = SQLiteStore.snapshot_from_path(database)
    try:
        release_fact = release_store.get_runtime_record(released["record_id"])
        assert release_fact is not None
        assert first["constraint_id"] in release_fact.causal_parents
    finally:
        release_store.close()
    final_history = _invoke(runner, ["task", "dependencies", target.id, "--json"])
    placement = final_history["data"][0]
    assert placement["active"] is False
    assert placement["release_refs"][0]["record_id"] == released["record_id"]
    assert _plugin_key_path(root_key_path).is_file()


def test_exact_link_replay_survives_release_claim_and_target_terminal(
    tmp_path, monkeypatch
):
    database, _root_key, target, prerequisite = _workspace(
        tmp_path, monkeypatch, target_outputs=("deliverable",)
    )
    runner = CliRunner()
    args = [
        "task",
        "link",
        target.id,
        "--depends-on",
        prerequisite.id,
        "--reason",
        "wait for source",
        "--idempotency-key",
        "dep-replay-after-state",
        "--json",
    ]
    original = _invoke(runner, args)["data"]
    store = SQLiteStore(database)
    try:
        store.append_runtime_record(create_terminal_record(prerequisite.id, "complete"))
        assert store.claim_contract(target.id, "worker:after-prerequisite") is not None
        store.append_runtime_record(create_terminal_record(target.id, "complete"))
    finally:
        store.close()
    replay = _invoke(runner, args)["data"]
    assert replay["record_id"] == original["record_id"]
    assert replay["candidate_id"] == original["candidate_id"]
    history = _invoke(runner, ["task", "dependencies", target.id, "--json"])
    assert history["data"][0]["observed_terminal"] == "complete"
    human = runner.invoke(_app(), ["task", "dependencies", target.id])
    assert human.exit_code == 0, human.output
    assert "prerequisite terminal: complete" in human.output


def test_duplicate_cycle_and_self_dependencies_fail_closed(tmp_path, monkeypatch):
    database, _root_key, target, prerequisite = _workspace(
        tmp_path, monkeypatch, target_outputs=("deliverable",)
    )
    runner = CliRunner()
    first = _invoke(
        runner,
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "wait",
            "--idempotency-key",
            "dep-first",
            "--json",
        ],
    )["data"]
    del first
    duplicate = runner.invoke(
        _app(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "duplicate",
            "--idempotency-key",
            "dep-second",
            "--json",
        ],
    )
    assert duplicate.exit_code == 1
    assert json.loads(duplicate.output)["ok"] is False

    cycle = runner.invoke(
        _app(),
        [
            "task",
            "link",
            prerequisite.id,
            "--depends-on",
            target.id,
            "--reason",
            "cycle",
            "--idempotency-key",
            "dep-cycle",
            "--json",
        ],
    )
    assert cycle.exit_code == 1
    assert json.loads(cycle.output)["ok"] is False

    self_link = runner.invoke(
        _app(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            target.id,
            "--reason",
            "self",
            "--idempotency-key",
            "dep-self",
            "--json",
        ],
    )
    assert self_link.exit_code == 1
    assert json.loads(self_link.output)["ok"] is False
    store = SQLiteStore.snapshot_from_path(database)
    try:
        assert (
            len(store.scan_runtime_records(record_type="claim.constraint.placed")) == 1
        )
    finally:
        store.close()


def test_redis_sync_pending_preserves_accepted_dependency_fact(tmp_path, monkeypatch):
    from aigineering.runtime_services import RedisSyncPending

    database, _root_key, target, prerequisite = _workspace(
        tmp_path, monkeypatch, target_outputs=("deliverable",)
    )
    calls = 0

    def fail_after_commit(_store, *, accepted=None):
        nonlocal calls
        calls += 1
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr("aigineering.task_dependencies.sync_runtime", fail_after_commit)
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "relay test",
            "--idempotency-key",
            "dep-relay-pending",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "redis_unavailable"
    store = SQLiteStore.snapshot_from_path(database)
    try:
        placements = store.scan_runtime_records(record_type="claim.constraint.placed")
        assert len(placements) == 1
        assert placements[0][1].payload["contract_id"] == target.id
        assert placements[0][1].payload["prerequisite_contract_id"] == prerequisite.id
    finally:
        store.close()


def test_same_raw_key_new_signed_intent_after_release_is_a_new_candidate(
    tmp_path, monkeypatch
):
    _database, _root_key, target, prerequisite = _workspace(
        tmp_path, monkeypatch, target_outputs=("deliverable",)
    )
    runner = CliRunner()
    first_args = [
        "task",
        "link",
        target.id,
        "--depends-on",
        prerequisite.id,
        "--reason",
        "first intent",
        "--idempotency-key",
        "operator-reuses-key",
        "--json",
    ]
    first = _invoke(runner, first_args)["data"]
    _invoke(
        runner,
        [
            "task",
            "unlink",
            target.id,
            "--link",
            first["constraint_id"],
            "--reason",
            "release first intent",
            "--idempotency-key",
            "operator-release",
            "--json",
        ],
    )
    second_args = list(first_args)
    second_args[second_args.index("first intent")] = "different signed intent"
    second = _invoke(runner, second_args)["data"]
    assert second["constraint_id"] != first["constraint_id"]
    assert second["candidate_id"] != first["candidate_id"]
    assert second["reason"] == "different signed intent"


def test_invalid_target_or_blank_key_fails_before_operator_identity(
    tmp_path, monkeypatch
):
    _database, root_key_path, target, prerequisite = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    invalid_target = runner.invoke(
        _app(),
        [
            "task",
            "link",
            "missing-target",
            "--depends-on",
            prerequisite.id,
            "--reason",
            "wait",
            "--json",
        ],
    )
    assert invalid_target.exit_code == 1
    assert json.loads(invalid_target.output)["error"]["code"] == "not_found"
    blank_key = runner.invoke(
        _app(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "wait",
            "--idempotency-key",
            " ",
            "--json",
        ],
    )
    assert blank_key.exit_code == 1
    assert json.loads(blank_key.output)["error"]["code"] == "invalid_input"
    self_link = runner.invoke(
        _app(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            target.id,
            "--reason",
            "static self dependency",
            "--json",
        ],
    )
    assert self_link.exit_code == 1
    assert json.loads(self_link.output)["error"]["code"] == "invalid_input"
    assert not _plugin_key_path(root_key_path).exists()
    store = SQLiteStore.snapshot_from_path(_database)
    try:
        assert not store.scan_runtime_records(record_type="claim.constraint.placed")
        assert not any(
            record.payload.get("actor_id") == "plugin:task.dependencies.operator.v1"
            for _revision, record in store.scan_runtime_records()
        )
    finally:
        store.close()


def test_held_target_can_be_linked_and_unlinked_without_releasing_hold(
    tmp_path, monkeypatch
):
    from aigineering.task_collaboration import publish_task_block

    database, _root_key, target, prerequisite = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    try:
        hold, _ = publish_task_block(store, target.id, "safe relink window")
    finally:
        store.close()

    runner = CliRunner()
    placement = _invoke(
        runner,
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "wait while relinking",
            "--json",
        ],
    )["data"]
    released = _invoke(
        runner,
        [
            "task",
            "unlink",
            target.id,
            "--link",
            placement["constraint_id"],
            "--reason",
            "replace prerequisite",
            "--json",
        ],
    )["data"]
    assert released["constraint_id"] == placement["constraint_id"]
    status_store = SQLiteStore.snapshot_from_path(database)
    try:
        status = project_task_status(target, status_store)
        holds = tuple(
            record
            for _rev, record in status_store.scan_runtime_records(
                record_type="claim.hold.placed"
            )
        )
    finally:
        status_store.close()
    assert holds and holds[0].id == hold["hold_id"]
    assert f"claim_hold:{hold['hold_id']}" in status["blockers"]
    assert not status["dependencies"]


def test_sqlite_claim_waits_for_exact_typed_prerequisite_completion(
    tmp_path, monkeypatch
):
    database, _root_key, target, prerequisite = _workspace(tmp_path, monkeypatch)
    _invoke(
        CliRunner(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "must finish first",
            "--json",
        ],
    )
    store = SQLiteStore(database)
    assert store.claim_contract(target.id, "worker:waiting") is None
    store.append_runtime_record(
        create_terminal_record(
            prerequisite.id,
            "complete",
            actor_id="test:fixture",
            reason="typed prerequisite completion",
        )
    )
    assert store.claim_contract(target.id, "worker:ready") is not None
    store.close()


def test_dependency_is_only_a_claim_gate_when_output_becomes_satisfied(
    tmp_path, monkeypatch
):
    database, _root_key, target, prerequisite = _workspace(
        tmp_path, monkeypatch, target_outputs=("deliverable",)
    )
    _invoke(
        CliRunner(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "claim waits for source",
            "--json",
        ],
    )
    store = SQLiteStore(database)
    store.add_asset(
        sign_asset(
            Asset(
                id="asset:dependency-output",
                name="deliverable",
                content="already available",
                origin="human",
            ),
            signed_by="human:fixture",
        )
    )
    store.close()

    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        status = project_task_status(target, snapshot)
        assert status["outputs_satisfied"] is True
        assert status["status"] == "completed"
        assert status["dependency_blockers"][0]["observed_terminal"] == "missing"
        assert not snapshot.scan_runtime_records(record_type="lifecycle.terminal")
    finally:
        snapshot.close()


def test_human_commands_show_copyable_placement_and_release_ids(tmp_path, monkeypatch):
    _database, _root_key, target, prerequisite = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    linked = runner.invoke(
        _app(),
        [
            "task",
            "link",
            target.id,
            "--depends-on",
            prerequisite.id,
            "--reason",
            "wait for review",
        ],
    )
    assert linked.exit_code == 0, linked.output
    assert target.id in linked.output and prerequisite.id in linked.output
    store = SQLiteStore(tmp_path / ".aig" / "store.db")
    try:
        placement = next(
            record
            for _revision, record in store.scan_runtime_records(
                record_type="claim.constraint.placed"
            )
        )
    finally:
        store.close()
    assert placement.id in linked.output

    history = runner.invoke(_app(), ["task", "dependencies", target.id])
    assert history.exit_code == 0, history.output
    assert placement.id in history.output
    assert "ACTIVE" in history.output

    invalid = runner.invoke(
        _app(),
        [
            "task",
            "unlink",
            target.id,
            "--link",
            "record:missing",
            "--reason",
            "no such link",
        ],
    )
    assert invalid.exit_code != 0
    assert "constraint" in invalid.output.lower() or "link" in invalid.output.lower()
