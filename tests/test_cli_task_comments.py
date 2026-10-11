"""Behavioral acceptance for the signed task comment CLI and service."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner
import pytest

from aigineering.cli.task_collaboration import register_task_collaboration_commands
from aigineering.core.ids import hash_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.types import Contract
from aigineering.task_views import project_task_status
from aigineering.task_collaboration import publish_task_comment


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_collaboration_commands(task)
    root.add_command(task)
    return root


def _workspace(tmp_path: Path, monkeypatch):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    store = SQLiteStore(database)
    ensure_local_domain(store)
    contract = Contract(
        id=hash_contract(
            name="comment-target",
            description="",
            inputs=[],
            outputs=[],
            activation="",
            budget=3,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name="comment-target",
        budget=3,
    )
    store.add_contract(contract)
    store.close()
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_store",
        lambda: SQLiteStore(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration.require_redis", lambda: None
    )
    monkeypatch.setattr("aigineering.task_collaboration.require_redis", lambda: None)

    class _Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        "aigineering.application.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", lambda *_a, **_k: None
    )
    return database, key_path, contract


def _plugin_key_path(root_key_path: Path) -> Path:
    from aigineering.core.ids import compute_content_hash

    actor_id = "plugin:task.collaboration.operator.v1"
    key_suffix = compute_content_hash(actor_id)[:16]
    return root_key_path.parent / f"plugin-{key_suffix}.ed25519"


def test_comment_publishes_signed_record_and_exact_replay_keeps_durable_time(
    tmp_path, monkeypatch
):
    database, root_key_path, contract = _workspace(tmp_path, monkeypatch)
    cli = _app()
    before_store = SQLiteStore.snapshot_from_path(database)
    before_status = project_task_status(contract, before_store)
    before_store.close()

    first = CliRunner().invoke(
        cli,
        [
            "task",
            "comment",
            contract.id,
            "Review note",
            "--idempotency-key",
            "note-1",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload["ok"] is True
    item = first_payload["data"]
    assert item["contract_id"] == contract.id
    assert item["text"] == "Review note"
    assert item["reply_to"] == ""
    assert item["actor_id"] == "plugin:task.collaboration.operator.v1"
    assert item["key_id"].startswith("plugin-")
    assert item["candidate_id"]
    assert item["recorded_at"]
    assert item["revision"] <= first_payload["revision"]
    assert _plugin_key_path(root_key_path).is_file()

    replay = CliRunner().invoke(
        cli,
        [
            "task",
            "comment",
            contract.id,
            "Review note",
            "--idempotency-key",
            "note-1",
            "--json",
        ],
    )
    assert replay.exit_code == 0, replay.output
    replay_item = json.loads(replay.output)["data"]
    assert replay_item == item

    after_store = SQLiteStore.snapshot_from_path(database)
    after_status = project_task_status(contract, after_store)
    after_store.close()
    assert after_status == before_status


def test_reused_idempotency_key_with_different_text_is_a_distinct_candidate(
    tmp_path, monkeypatch
):
    database, _root_key_path, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    cli = _app()
    first = runner.invoke(
        cli,
        [
            "task",
            "comment",
            contract.id,
            "First text",
            "--idempotency-key",
            "reused-key",
            "--json",
        ],
    )
    second = runner.invoke(
        cli,
        [
            "task",
            "comment",
            contract.id,
            "Different text",
            "--idempotency-key",
            "reused-key",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_data = json.loads(first.output)["data"]
    second_data = json.loads(second.output)["data"]
    assert first_data["candidate_id"] != second_data["candidate_id"]
    assert first_data["record_id"] != second_data["record_id"]
    assert first_data["text"] == "First text"
    assert second_data["text"] == "Different text"

    store = SQLiteStore.snapshot_from_path(database)
    comments = store.scan_runtime_records(record_type="task.commented")
    store.close()
    assert len(comments) == 2


def test_comment_reply_targets_committed_comment_and_list_filters_by_revision(
    tmp_path, monkeypatch
):
    database, _root_key_path, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    cli = _app()
    parent = runner.invoke(cli, ["task", "comment", contract.id, "Parent", "--json"])
    assert parent.exit_code == 0, parent.output
    parent_data = json.loads(parent.output)["data"]

    reply = runner.invoke(
        cli,
        [
            "task",
            "comment",
            contract.id,
            "Reply",
            "--reply-to",
            parent_data["record_id"],
            "--json",
        ],
    )
    assert reply.exit_code == 0, reply.output
    reply_data = json.loads(reply.output)["data"]
    assert reply_data["reply_to"] == parent_data["record_id"]

    store = SQLiteStore.snapshot_from_path(database)
    record = store.get_runtime_record(reply_data["record_id"])
    assert record is not None
    assert parent_data["record_id"] in record.causal_parents
    store.close()

    all_comments = runner.invoke(cli, ["task", "comments", contract.id, "--json"])
    assert all_comments.exit_code == 0, all_comments.output
    rows = json.loads(all_comments.output)["data"]
    assert [row["record_id"] for row in rows] == [
        parent_data["record_id"],
        reply_data["record_id"],
    ]

    after_parent = runner.invoke(
        cli,
        [
            "task",
            "comments",
            contract.id,
            "--after",
            str(parent_data["revision"]),
            "--limit",
            "1",
            "--json",
        ],
    )
    assert after_parent.exit_code == 0, after_parent.output
    filtered = json.loads(after_parent.output)["data"]
    assert [row["record_id"] for row in filtered] == [reply_data["record_id"]]


def test_invalid_text_unknown_target_and_reply_do_not_create_operator_key(
    tmp_path, monkeypatch
):
    database, root_key_path, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    cli = _app()
    key_path = _plugin_key_path(root_key_path)
    assert not key_path.exists()

    invalid = runner.invoke(cli, ["task", "comment", contract.id, "   ", "--json"])
    assert invalid.exit_code == 1
    assert json.loads(invalid.output)["error"]["code"] == "invalid_input"
    assert not key_path.exists()

    unknown_target = runner.invoke(
        cli, ["task", "comment", "missing", "Comment", "--json"]
    )
    assert unknown_target.exit_code == 1
    assert json.loads(unknown_target.output)["error"]["code"] == "not_found"
    assert not key_path.exists()

    unknown_reply = runner.invoke(
        cli,
        ["task", "comment", contract.id, "Comment", "--reply-to", "missing", "--json"],
    )
    assert unknown_reply.exit_code == 1
    assert json.loads(unknown_reply.output)["error"]["code"] == "not_found"
    assert not key_path.exists()

    oversized = "é" * (32 * 1024 + 1)
    rejected = runner.invoke(cli, ["task", "comment", contract.id, oversized, "--json"])
    assert rejected.exit_code == 1
    assert json.loads(rejected.output)["error"]["code"] == "invalid_input"
    assert not key_path.exists()


def test_comment_limit_options_reject_out_of_range_values():
    cli = _app()
    for args in (
        ["task", "comments", "contract", "--after", "-1", "--json"],
        ["task", "comments", "contract", "--limit", "0", "--json"],
        ["task", "comments", "contract", "--limit", "1001", "--json"],
    ):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 2


def test_invalid_text_fails_before_opening_either_store(tmp_path, monkeypatch):
    def fail_if_opened():
        raise AssertionError("invalid comment text must fail before Store open")

    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_snapshot", fail_if_opened
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_store", fail_if_opened
    )
    result = CliRunner().invoke(_app(), ["task", "comment", "contract", "  ", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_input"


def test_blank_idempotency_key_does_not_provision_operator_authority(
    tmp_path, monkeypatch
):
    database, root_key_path, contract = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    original_authorizations = store.scan_runtime_records(record_type="actor.authorized")
    with pytest.raises(ValueError, match="non-blank"):
        publish_task_comment(store, contract.id, "text", idempotency_key="   ")
    assert store.scan_runtime_records(record_type="actor.authorized") == (
        original_authorizations
    )
    assert not _plugin_key_path(root_key_path).exists()
    store.close()


def test_uninitialized_domain_is_distinguished_from_unknown_task(tmp_path, monkeypatch):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    initialized_store = SQLiteStore(database)
    initialized_store.close()
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration.require_redis", lambda: None
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration.collaboration_service",
        lambda _store: type("Collaboration", (), {"sync": lambda self: None})(),
    )

    result = CliRunner().invoke(
        _app(), ["task", "comment", "missing", "text", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "workspace_not_initialized"
    assert not root_key_path.exists()


def test_postcommit_redis_failure_reports_retained_candidate_and_replay_is_stable(
    tmp_path, monkeypatch
):
    database, _root_key_path, contract = _workspace(tmp_path, monkeypatch)
    from aigineering.cli.main import cli
    from aigineering.runtime_services import RedisSyncPending

    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
    sync_calls = 0

    def pending_after_comment(_store, *, accepted=None):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise RedisSyncPending(accepted=accepted)

    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", pending_after_comment
    )
    args = [
        "task",
        "comment",
        contract.id,
        "Durable note",
        "--idempotency-key",
        "stable-note",
        "--json",
    ]
    failed = CliRunner().invoke(cli, args)
    assert failed.exit_code == 1, failed.output
    failure = json.loads(failed.output)
    assert failure["error"] == "redis_sync_pending"
    assert failure["facts_retained"] is True
    assert failure["candidate_accepted"] is True

    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", lambda *_a, **_k: None
    )
    replay = CliRunner().invoke(cli, args)
    assert replay.exit_code == 0, replay.output
    replay_item = json.loads(replay.output)["data"]
    store = SQLiteStore.snapshot_from_path(database)
    comments = store.scan_runtime_records(record_type="task.commented")
    store.close()
    assert len(comments) == 1
    assert replay_item["record_id"] == comments[0][1].id
    assert replay_item["recorded_at"] == comments[0][1].recorded_at
    assert replay_item["candidate_id"] == comments[0][1].payload["candidate_id"]
    assert replay_item["revision"] == comments[0][0]


def test_registration_after_main_does_not_inherit_redis_protection_wrapper(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    from aigineering.cli.main import cli

    main_comment = cli.commands["task"].commands["comment"]
    assert getattr(main_comment.callback, "_redis_protected", False)
    isolated = _app()
    isolated_comment = isolated.commands["task"].commands["comment"]
    assert not getattr(isolated_comment.callback, "_redis_protected", False)

    result = CliRunner().invoke(
        isolated,
        ["task", "comment", "missing", "note"],
    )
    assert result.exit_code == 1
    assert "unknown Contract" in result.output
