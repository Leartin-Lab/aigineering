"""Signed CLI acceptance for bounded task edits through handoff successors."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner
import pytest

from aigineering.cli.task_handoff import register_task_handoff_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.ids import compute_content_hash
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_plugin_publisher,
    load_actor_signer,
)
from aigineering.plugins.collaboration import project_task_holds
from aigineering.protocol.effect_builders import (
    actor_revocation_effect,
    contract_declaration_effect,
)
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.task_collaboration import publish_task_block, publish_task_unblock
from aigineering.task_handoff import validate_task_edit
from aigineering.task_views import project_task_status
from aigineering.runtime_services import RedisSyncPending


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_handoff_commands(task)
    root.add_command(task)
    return root


def _workspace(tmp_path: Path, monkeypatch):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key = database.parent / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    signer = load_actor_signer(root_key)
    actor_key = next(
        key for key in genesis.root_keys if key.public_key == signer.signer_id
    )
    source = build_control_plane_contract(
        name="café draft",
        description="draft body",
        outputs=("draft", "final"),
        budget=8,
    )
    decision = publish_effect(
        store,
        store,
        genesis,
        actor_key,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-edit-source",
    )
    assert decision.accepted
    store.close()

    monkeypatch.setattr(
        "aigineering.cli.task_handoff._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_handoff._persistent_store", lambda: SQLiteStore(database)
    )
    for target in (
        "aigineering.cli.task_handoff.require_redis",
        "aigineering.task_handoff.require_redis",
        "aigineering.task_collaboration.require_redis",
        "aigineering.application.require_redis",
    ):
        monkeypatch.setattr(target, lambda *_a, **_k: None)

    class _Redis:
        def sync(self):
            return None

    for target in (
        "aigineering.application.collaboration_service",
        "aigineering.cli.task_handoff.collaboration_service",
        "aigineering.task_handoff.collaboration_service",
    ):
        monkeypatch.setattr(target, lambda _store: _Redis())
    monkeypatch.setattr("aigineering.task_handoff.sync_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", lambda *_a, **_k: None
    )
    return database, root_key, source


def _invoke(runner: CliRunner, argv: list[str]) -> dict:
    result = runner.invoke(_app(), argv)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    return envelope


def _edit_key_path(root_key: Path) -> Path:
    actor = "plugin:task.edit.operator.v1"
    return root_key.parent / f"plugin-{compute_content_hash(actor)[:16]}.ed25519"


def test_title_body_edit_copies_source_holds_and_exact_replay_after_release(
    tmp_path, monkeypatch
):
    database, _root_key, source = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    placed = publish_task_block(
        store, source.id, "waiting for review", idempotency_key="hold-edit"
    )[0]
    store.close()
    hold_id = placed["hold_id"]

    args = [
        "task",
        "edit",
        source.id,
        "--title",
        "revised title",
        "--body",
        "revised body",
        "--reason",
        "Correct the task brief",
        "--idempotency-key",
        "edit-stable-key",
        "--json",
    ]
    original = _invoke(CliRunner(), args)["data"]
    assert original["record_type"] == "task.handed_off"
    assert original["contract_id"] == source.id
    assert original["source_status"] == "cancelled"
    assert original["idempotency_key"] == "edit-stable-key"
    assert original["edit"]["changed_fields"] == ["title", "body"]
    assert original["source_hold_ids"] == [hold_id]
    mapping = original["edit"]["hold_mapping"]
    assert len(mapping) == 1 and mapping[0]["source_hold_id"] == hold_id

    store = SQLiteStore.snapshot_from_path(database)
    successor = store.get_contract(original["successor_id"])
    assert successor is not None
    assert successor.name == "revised title"
    assert successor.description == "revised body"
    assert successor.parent_id == source.id
    assert successor.inputs == source.inputs
    assert successor.tool_scope == source.tool_scope
    assert successor.acceptance_policy == source.acceptance_policy
    assert set(successor.outputs) <= set(source.outputs)
    source_holds = project_task_holds(
        (record for _revision, record in store.scan_runtime_records()),
        contract_id=source.id,
    )
    successor_holds = project_task_holds(
        (record for _revision, record in store.scan_runtime_records()),
        contract_id=successor.id,
    )
    assert [record.id for record in source_holds] == [hold_id]
    assert [record.payload["reason"] for record in successor_holds] == [
        "waiting for review"
    ]
    assert successor_holds[0].id == mapping[0]["successor_hold_id"]
    assert project_task_status(source, store)["status"] == "cancelled"
    store.close()

    # The source hold can be explicitly released after cancellation. Replay
    # still proves and returns the old signed edit instead of comparing against
    # the now-changed open-hold set.
    store = SQLiteStore(database)
    publish_task_unblock(
        store,
        source.id,
        hold_id,
        "source hold no longer needed",
        idempotency_key="release-source-edit-hold",
    )
    store.close()
    replay = _invoke(CliRunner(), args)["data"]
    assert replay == original
    replay_with_returned_snapshot = _invoke(
        CliRunner(),
        [
            *args[:-1],
            "--source-assignment-id",
            original["source_assignment_id"],
            "--source-hold-id",
            hold_id,
            "--json",
        ],
    )["data"]
    assert replay_with_returned_snapshot == original

    history = _invoke(CliRunner(), ["task", "edits", source.id, "--json"])
    assert [item["record_id"] for item in history["data"]] == [original["record_id"]]
    assert history["data"][0]["edit"] == original["edit"]


def test_assigned_source_edit_inherits_exact_assignment_and_replays(
    tmp_path, monkeypatch
):
    database, _root_key, source = _workspace(tmp_path, monkeypatch)
    import aigineering.task_assignment as assignment_service

    monkeypatch.setattr(assignment_service, "require_redis", lambda: None)
    monkeypatch.setattr(assignment_service, "sync_runtime", lambda *_a, **_k: None)
    store = SQLiteStore(database)
    store.register_worker(WorkerRegistration("worker:edit-source", capacity=2))
    assignment, _ = assignment_service.publish_task_assignment(
        store,
        source.id,
        "worker:edit-source",
        "assign source before edit",
        idempotency_key="edit-source-assignment",
    )
    source_assignment_id = assignment["assignment_id"]
    store.close()

    args = [
        "task",
        "edit",
        source.id,
        "--title",
        "assigned source successor",
        "--reason",
        "Preserve the selected worker",
        "--idempotency-key",
        "assigned-source-edit",
        "--json",
    ]
    original = _invoke(CliRunner(), args)["data"]
    assert original["source_assignment_id"] == source_assignment_id
    assert original["assignment_mode"] == "inherit"
    assert original["worker_id"] == "worker:edit-source"
    assert original["successor_assignment_id"]
    replay = _invoke(CliRunner(), args)["data"]
    assert replay == original

    store = SQLiteStore.snapshot_from_path(database)
    try:
        successor = store.get_contract(original["successor_id"])
        assert successor is not None
        assert successor.name == "assigned source successor"
        assert store.get_runtime_revision() > 0
    finally:
        store.close()


def test_explicit_empty_body_is_a_replacement_and_noop_fails_before_identity(
    tmp_path, monkeypatch
):
    database, root_key, source = _workspace(tmp_path, monkeypatch)
    edit_key = _edit_key_path(root_key)
    assert not edit_key.exists()
    store = SQLiteStore.snapshot_from_path(database)
    before_revision = store.get_runtime_revision()
    store.close()

    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "edit",
            source.id,
            "--title",
            "cafe\u0301 draft",
            "--reason",
            "No change",
            "--json",
        ],
    )
    assert result.exit_code != 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is False
    assert "no-op" in envelope["error"]["message"]
    assert not edit_key.exists()
    store = SQLiteStore.snapshot_from_path(database)
    assert store.get_runtime_revision() == before_revision
    store.close()
    accepted = _invoke(
        CliRunner(),
        [
            "task",
            "edit",
            source.id,
            "--body",
            "",
            "--reason",
            "Remove the body",
            "--json",
        ],
    )["data"]
    store = SQLiteStore.snapshot_from_path(database)
    successor = store.get_contract(accepted["successor_id"])
    assert successor is not None and successor.description == ""
    assert successor.name == source.name
    store.close()


def test_stale_explicit_snapshot_fails_before_edit_identity_or_candidate(
    tmp_path, monkeypatch
):
    database, root_key, source = _workspace(tmp_path, monkeypatch)
    edit_key = _edit_key_path(root_key)
    before = SQLiteStore.snapshot_from_path(database)
    before_revision = before.get_runtime_revision()
    before.close()
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "edit",
            source.id,
            "--title",
            "new title",
            "--reason",
            "Change the title",
            "--source-assignment-id",
            "record:stale-assignment",
            "--json",
        ],
    )
    assert result.exit_code != 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_input"
    assert "assignment" in envelope["error"]["message"]
    assert not edit_key.exists()
    after = SQLiteStore.snapshot_from_path(database)
    assert after.get_runtime_revision() == before_revision
    assert not any(
        record.payload.get("actor_id") == "plugin:task.edit.operator.v1"
        for _revision, record in after.scan_runtime_records(
            record_type="candidate.received"
        )
    )
    after.close()


def test_edit_text_validation_rejects_bad_utf8_and_oversize_before_setup(
    tmp_path, monkeypatch
):
    database, root_key, source = _workspace(tmp_path, monkeypatch)
    edit_key = _edit_key_path(root_key)
    store = SQLiteStore.snapshot_from_path(database)
    for value in ("bad\ud800", "x" * 65537):
        with pytest.raises(ValueError):
            validate_task_edit(
                store, source.id, "edit safely", title=value, idempotency_key="bad"
            )
    assert not edit_key.exists()
    store.close()


def test_postcommit_redis_pending_reports_retained_successor_and_history(
    tmp_path, monkeypatch
):
    database, _root_key, source = _workspace(tmp_path, monkeypatch)

    def sync(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr("aigineering.task_handoff.sync_runtime", sync)
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "edit",
            source.id,
            "--title",
            "committed despite relay outage",
            "--reason",
            "Preserve the accepted edit",
            "--idempotency-key",
            "edit-relay-pending",
            "--json",
        ],
    )
    assert result.exit_code != 0
    envelope = json.loads(result.output)
    assert envelope["error"] == "redis_sync_pending"
    assert envelope["candidate_accepted"] is True
    assert envelope["facts_retained"] is True
    item = envelope["data"]
    assert item["successor_id"]
    assert item["record_type"] == "task.handed_off"
    store = SQLiteStore.snapshot_from_path(database)
    try:
        assert store.get_contract(item["successor_id"]).name == (
            "committed despite relay outage"
        )
        links = [
            record
            for _revision, record in store.scan_runtime_records(
                record_type="task.handed_off"
            )
            if record.payload.get("candidate_id") == item["candidate_id"]
        ]
        assert len(links) == 1
    finally:
        store.close()


def test_existing_edit_operator_without_task_edit_is_not_rebound(tmp_path, monkeypatch):
    database, root_key, source = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    ensure_local_plugin_publisher(
        store,
        "task.edit.operator.v1",
        ("contract.cancel", "contract.publish", "task.block", "task.handoff"),
    )
    before = store.get_runtime_revision()
    store.close()

    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "edit",
            source.id,
            "--title",
            "must not be published",
            "--reason",
            "The operator lacks task.edit",
            "--json",
        ],
    )
    assert result.exit_code != 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] in {"invalid_input", "rejected"}
    assert _edit_key_path(root_key).exists()

    store = SQLiteStore.snapshot_from_path(database)
    try:
        assert store.get_runtime_revision() == before
        assert store.get_contract(source.id).name == source.name
        assert not tuple(store.scan_runtime_records(record_type="task.handed_off"))
        assert not any(
            record.payload.get("actor_id") == "plugin:task.edit.operator.v1"
            for _revision, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
        )
    finally:
        store.close()


def test_revoked_edit_operator_cannot_replay_or_reprovision_successor(
    tmp_path, monkeypatch
):
    database, root_key, source = _workspace(tmp_path, monkeypatch)
    args = [
        "task",
        "edit",
        source.id,
        "--title",
        "committed title",
        "--reason",
        "Authorized edit",
        "--idempotency-key",
        "revoked-edit-replay",
        "--json",
    ]
    original = _invoke(CliRunner(), args)["data"]
    edit_actor_id = "plugin:task.edit.operator.v1"

    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    root_signer = load_actor_signer(root_key)
    root_key_record = next(
        key for key in genesis.root_keys if key.public_key == root_signer.signer_id
    )
    revoke = publish_effect(
        store,
        store,
        genesis,
        root_key_record,
        root_signer,
        actor_revocation_effect(edit_actor_id, original["key_id"], "operator removed"),
        idempotency_key="revoke-edit-operator",
    )
    assert revoke.accepted
    before_revision = store.get_runtime_revision()
    before_contract_ids = {item.id for item in store.get_all_contracts()}
    store.close()

    replay = CliRunner().invoke(_app(), args)
    assert replay.exit_code != 0
    envelope = json.loads(replay.output)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "rejected"

    store = SQLiteStore.snapshot_from_path(database)
    try:
        assert store.get_runtime_revision() == before_revision
        assert {item.id for item in store.get_all_contracts()} == before_contract_ids
        assert any(
            record.payload.get("actor_id") == edit_actor_id
            and record.payload.get("key_id") == original["key_id"]
            for _revision, record in store.scan_runtime_records(
                record_type="actor.revoked"
            )
        )
        assert (
            sum(
                1
                for _revision, record in store.scan_runtime_records(
                    record_type="actor.authorized"
                )
                if record.payload.get("actor_id") == edit_actor_id
            )
            == 1
        )
        assert (
            sum(
                1
                for _revision, record in store.scan_runtime_records(
                    record_type="candidate.received"
                )
                if record.payload.get("actor_id") == edit_actor_id
            )
            == 1
        )
        assert (
            len(
                tuple(
                    record
                    for _revision, record in store.scan_runtime_records(
                        record_type="task.handed_off"
                    )
                    if record.payload.get("edit") is not None
                )
            )
            == 1
        )
        assert store.get_contract(original["successor_id"]) is not None
    finally:
        store.close()
