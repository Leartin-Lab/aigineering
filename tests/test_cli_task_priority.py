"""Behavioral tests for signed task priority commands and replay."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner
import pytest

from aigineering.cli.task_priority import register_task_priority_commands
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import compute_content_hash
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.task_publication import publish_task_contract
from aigineering.protocol.types import Asset
from aigineering.core.provenance import sign_asset


def _cli() -> Group:
    root = Group()
    task = Group("task")
    register_task_priority_commands(task)
    root.add_command(task)
    return root


def _workspace(tmp_path: Path, monkeypatch, *, outputs=()):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    store = SQLiteStore(database)
    ensure_local_domain(store)
    contract = build_control_plane_contract(
        name="priority-task",
        description="",
        inputs=(),
        outputs=tuple(outputs),
        budget=3,
        activation="",
        tool_scope=(),
        labels=(),
    )
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )
    publish_task_contract(store, contract)
    store.close()
    monkeypatch.setattr(
        "aigineering.cli.task_priority._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_priority._persistent_store",
        lambda: SQLiteStore(database),
    )

    class _Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr("aigineering.cli.task_priority.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.application.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.application.collaboration_service", lambda _store: _Collaboration()
    )
    monkeypatch.setattr(
        "aigineering.cli.task_priority.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.task_priority.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.task_priority.sync_runtime", lambda *_a, **_k: None
    )
    return database, key_path, contract


def _plugin_key(key_path: Path) -> Path:
    actor_id = "plugin:task.priority.operator.v1"
    return key_path.parent / f"plugin-{compute_content_hash(actor_id)[:16]}.ed25519"


def _json(runner, args):
    result = runner.invoke(_cli(), args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_cli_accepts_negative_values_requires_exact_head_and_clears_to_zero(
    tmp_path, monkeypatch
):
    database, root_key, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    first = _json(
        runner,
        ["task", "priority", contract.id, "-10", "--reason", "deprioritize", "--json"],
    )
    row = first["data"]
    assert first["ok"] is True
    assert row["priority"] == -10
    assert row["previous_priority_id"] == ""
    assert row["record_type"] == "task.priority.changed"
    assert row["actor_id"] == "plugin:task.priority.operator.v1"
    assert row["candidate_id"] and row["key_id"]
    assert _plugin_key(root_key).is_file()

    missing_previous = runner.invoke(
        _cli(),
        ["task", "priority", contract.id, "12", "--reason", "raise", "--json"],
    )
    assert missing_previous.exit_code == 1
    assert json.loads(missing_previous.output)["error"]["code"] == "invalid_input"

    second = _json(
        runner,
        [
            "task",
            "priority",
            contract.id,
            "12",
            "--previous",
            row["record_id"],
            "--reason",
            "raise",
            "--json",
        ],
    )["data"]
    assert second["previous_priority_id"] == row["record_id"]
    cleared = _json(
        runner,
        [
            "task",
            "priority",
            contract.id,
            "0",
            "--previous",
            second["record_id"],
            "--reason",
            "clear",
            "--json",
        ],
    )["data"]
    assert cleared["priority"] == 0

    listing = _json(runner, ["task", "priorities", contract.id, "--json"])
    assert listing["data"]["priority"] == 0
    assert listing["data"]["head_priority_id"] == cleared["record_id"]
    assert [item["priority"] for item in listing["data"]["history"]] == [-10, 12, 0]
    assert listing["data"]["history"][-1]["revision"] <= listing["revision"]


def test_single_priority_missing_contract_uses_not_found_without_candidate(
    tmp_path, monkeypatch
):
    database, root_key, _contract_id = _workspace(tmp_path, monkeypatch)
    missing = "task:v6:priority-target-does-not-exist"
    before = SQLiteStore.snapshot_from_path(database)
    try:
        candidate_receipts_before = before.scan_runtime_records(
            record_type="candidate.received"
        )
    finally:
        before.close()
    result = CliRunner().invoke(
        _cli(),
        ["task", "priority", missing, "17", "--reason", "missing target", "--json"],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "not_found"
    assert not _plugin_key(root_key).exists()
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert snapshot.scan_runtime_records(record_type="task.priority.changed") == []
        assert (
            snapshot.scan_runtime_records(record_type="candidate.received")
            == candidate_receipts_before
        )
    finally:
        snapshot.close()


def test_human_priority_history_has_copyable_full_head_and_provenance(
    tmp_path, monkeypatch
):
    _database, _root_key, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    changed = runner.invoke(
        _cli(),
        ["task", "priority", contract.id, "-4", "--reason", "human view"],
    )
    assert changed.exit_code == 0, changed.output
    listed = runner.invoke(_cli(), ["task", "priorities", contract.id])
    assert listed.exit_code == 0, listed.output
    assert contract.id in listed.output
    assert "--previous record:" in listed.output
    assert "actor=plugin:task.priority.operator.v1" in listed.output
    assert "candidate=candidate:v1:" in listed.output


def test_noop_stale_bad_range_and_typo_do_not_create_priority_identity(
    tmp_path, monkeypatch
):
    database, root_key, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    first = _json(
        runner,
        ["task", "priority", contract.id, "10", "--reason", "raise", "--json"],
    )["data"]
    store = SQLiteStore.snapshot_from_path(database)
    before_auth = [
        item[1].id
        for item in store.scan_runtime_records(record_type="actor.authorized")
        if item[1].payload.get("actor_id") == "plugin:task.priority.operator.v1"
    ]
    store.close()
    bad_calls = [
        [
            "task",
            "priority",
            contract.id,
            "10",
            "--previous",
            first["record_id"],
            "--reason",
            "same",
            "--json",
        ],
        [
            "task",
            "priority",
            contract.id,
            "11",
            "--previous",
            "record:stale",
            "--reason",
            "stale",
            "--json",
        ],
        ["task", "priority", contract.id, "101", "--reason", "range", "--json"],
        ["task", "priority", contract.id, "--oops", "--reason", "typo", "--json"],
    ]
    for args in bad_calls:
        result = runner.invoke(_cli(), args)
        assert result.exit_code != 0
    store = SQLiteStore.snapshot_from_path(database)
    try:
        assert len(store.scan_runtime_records(record_type="task.priority.changed")) == 1
        after_auth = [
            item
            for item in store.scan_runtime_records(record_type="actor.authorized")
            if item[1].payload.get("actor_id") == "plugin:task.priority.operator.v1"
        ]
        assert [item[1].id for item in after_auth] == before_auth
    finally:
        store.close()
    assert _plugin_key(root_key).is_file()


def test_exact_signed_replay_survives_new_head_and_terminal_but_changed_key_does_not(
    tmp_path, monkeypatch
):
    database, _root_key, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    args = [
        "task",
        "priority",
        contract.id,
        "7",
        "--reason",
        "raise",
        "--idempotency-key",
        "priority-1",
        "--json",
    ]
    original = _json(runner, args)["data"]
    later = _json(
        runner,
        [
            "task",
            "priority",
            contract.id,
            "8",
            "--previous",
            original["record_id"],
            "--reason",
            "later",
            "--idempotency-key",
            "priority-2",
            "--json",
        ],
    )["data"]
    store = SQLiteStore(database)
    store.append_runtime_record(
        create_terminal_record(contract.id, "complete", reason="test terminal")
    )
    store.close()

    replay = _json(runner, args)["data"]
    assert replay == original
    wrong_key = runner.invoke(
        _cli(),
        [
            "task",
            "priority",
            contract.id,
            "7",
            "--reason",
            "raise",
            "--previous",
            original["previous_priority_id"],
            "--idempotency-key",
            "different-key",
            "--json",
        ],
    )
    assert wrong_key.exit_code == 1
    assert json.loads(wrong_key.output)["error"]["code"] == "invalid_input"
    wrong_intent = runner.invoke(
        _cli(),
        [
            "task",
            "priority",
            contract.id,
            "9",
            "--reason",
            "same key, changed signed intent",
            "--idempotency-key",
            "priority-1",
            "--json",
        ],
    )
    assert wrong_intent.exit_code == 1
    assert json.loads(wrong_intent.output)["error"]["code"] == "invalid_input"

    store = SQLiteStore.snapshot_from_path(database)
    try:
        priorities = store.scan_runtime_records(record_type="task.priority.changed")
        assert [item.payload["candidate_id"] for _, item in priorities] == [
            original["candidate_id"],
            later["candidate_id"],
        ]
    finally:
        store.close()


def test_revoked_priority_signer_cannot_replay_old_candidate(tmp_path, monkeypatch):
    from aigineering.local_identity import _local_root_publisher
    from aigineering.protocol.effect_builders import actor_revocation_effect

    database, _root_key, contract = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    args = [
        "task",
        "priority",
        contract.id,
        "5",
        "--reason",
        "replay after revocation",
        "--idempotency-key",
        "priority-revoked-replay",
        "--json",
    ]
    initial = _json(runner, args)["data"]
    store = SQLiteStore(database)
    genesis, root_publisher = _local_root_publisher(store)
    actor_id = "plugin:task.priority.operator.v1"
    key_id = f"plugin-{compute_content_hash(actor_id)[:16]}"
    decision = root_publisher.publish(
        (actor_revocation_effect(actor_id, key_id, "priority operator retired"),),
        idempotency_key="revoke-priority-operator",
    )
    assert decision.accepted
    store.close()

    replay = runner.invoke(_cli(), args)
    assert replay.exit_code == 1
    assert json.loads(replay.output)["error"]["code"] == "invalid_input"
    store = SQLiteStore.snapshot_from_path(database)
    try:
        facts = store.scan_runtime_records(record_type="task.priority.changed")
        assert len(facts) == 1 and facts[0][1].id == initial["record_id"]
    finally:
        store.close()


def test_invalid_bool_and_satisfied_output_reject_before_identity(
    tmp_path, monkeypatch
):
    database, root_key, contract = _workspace(tmp_path, monkeypatch, outputs=("out",))
    from aigineering.task_priority import validate_task_priority

    store = SQLiteStore(database)
    with pytest.raises(ValueError, match="integer"):
        validate_task_priority(store, contract.id, True, "bad")
    for malformed_head in (False, 0, [], {}):
        with pytest.raises(ValueError, match="previous_priority_id"):
            validate_task_priority(
                store,
                contract.id,
                2,
                "bad head type",
                previous_priority_id=malformed_head,
            )
        from aigineering.task_priority import publish_task_priority

        monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
        with pytest.raises(ValueError, match="previous_priority_id"):
            publish_task_priority(
                store,
                contract.id,
                2,
                "bad head type",
                previous_priority_id=malformed_head,
            )
    store.add_asset(
        sign_asset(
            Asset(
                id="asset:priority-output",
                name="out",
                content="done",
                created_by=contract.id,
                origin="human",
            ),
            signed_by="human:fixture",
        )
    )
    store.close()
    result = CliRunner().invoke(
        _cli(),
        ["task", "priority", contract.id, "2", "--reason", "update", "--json"],
    )
    assert result.exit_code == 1, result.output
    assert "completed" in json.loads(result.output)["error"]["message"]
    store = SQLiteStore.snapshot_from_path(database)
    try:
        assert not store.scan_runtime_records(record_type="task.priority.changed")
        assert not [
            item
            for item in store.scan_runtime_records(record_type="actor.authorized")
            if item[1].payload.get("actor_id") == "plugin:task.priority.operator.v1"
        ]
    finally:
        store.close()
    assert not _plugin_key(root_key).exists()
