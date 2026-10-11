"""Acceptance for signed task handoff application and CLI behavior."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner

from aigineering.cli.task_handoff import register_task_handoff_commands
from aigineering.core.ids import compute_content_hash, hash_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.effect_builders import task_handoff_effect
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.plugins.collaboration import project_task_holds
from aigineering.task_views import project_task_status


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_handoff_commands(task)
    root.add_command(task)
    return root


def _workspace(tmp_path: Path, monkeypatch):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    store = SQLiteStore(database)
    ensure_local_domain(store)
    source = Contract(
        id=hash_contract(
            name="handoff-source",
            description="source for handoff",
            inputs=(),
            outputs=("draft", "final"),
            activation="",
            budget=5,
            tool_scope=(),
            labels=(),
            origin="human",
        ),
        name="handoff-source",
        description="source for handoff",
        outputs=("draft", "final"),
        budget=5,
    )
    store.add_contract(source)
    store.close()
    monkeypatch.setattr(
        "aigineering.cli.task_handoff._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_handoff._persistent_store",
        lambda: SQLiteStore(database),
    )
    monkeypatch.setattr("aigineering.cli.task_handoff.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.task_handoff.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.application.require_redis", lambda: None)

    class _Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        "aigineering.application.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_handoff.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.task_handoff.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr("aigineering.task_handoff.sync_runtime", lambda *_a, **_k: None)
    return database, root_key_path, source


def _plugin_key_path(root_key_path: Path) -> Path:
    actor_id = "plugin:task.handoff.operator.v1"
    suffix = compute_content_hash(actor_id)[:16]
    return root_key_path.parent / f"plugin-{suffix}.ed25519"


def _invoke_json(runner, args):
    result = runner.invoke(_app(), args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    return data


def test_handoff_signed_atomic_successor_optional_hold_and_history(
    tmp_path, monkeypatch
):
    database, root_key_path, source = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    store = SQLiteStore.snapshot_from_path(database)
    claimed = store.claim_contract(source.id, "worker:active-before-handoff")
    assert claimed is not None
    store.close()

    first = _invoke_json(
        runner,
        [
            "task",
            "handoff",
            source.id,
            "--reason",
            "Split unfinished work",
            "--hold-reason",
            "Wait for evidence",
            "--idempotency-key",
            "handoff-1",
            "--json",
        ],
    )
    item = first["data"]
    assert item["record_type"] == "task.handed_off"
    assert item["contract_id"] == source.id
    assert item["successor_id"]
    assert item["candidate_id"]
    assert item["actor_id"] == "plugin:task.handoff.operator.v1"
    assert item["key_id"].startswith("plugin-")
    assert item["hold_id"]
    assert item["budget"] > 0
    assert _plugin_key_path(root_key_path).is_file()

    store = SQLiteStore.snapshot_from_path(database)
    successor = store.get_contract(item["successor_id"])
    assert successor is not None
    assert successor.parent_id == source.id
    assert set(successor.outputs) <= set(source.outputs)
    assert project_task_status(source, store)["status"] == "cancelled"
    assert item["hold_id"] in {
        record.id
        for record in project_task_holds(
            (record for _, record in store.scan_runtime_records()),
            contract_id=successor.id,
        )
    }
    assert store.get_claim(source.id) is None
    store.close()

    history = _invoke_json(runner, ["task", "handoffs", source.id, "--json"])
    assert history["data"] == [item]
    assert history["data"][0]["record_id"] == item["record_id"]


def test_handoff_history_json_preserves_nested_durable_evidence_as_objects(
    tmp_path, monkeypatch
):
    database, _root_key_path, source = _workspace(tmp_path, monkeypatch)
    returned = {
        "donor_contract_id": "task:donor",
        "amount": 2,
        "record_id": "record:return",
    }
    repair = {
        "task:repair-child": [
            {
                "output_name": "draft",
                "asset_ids": ["asset:repair"],
                "proof_record_ids": ["record:repair-proof"],
            }
        ]
    }
    retained = {
        "draft": {
            "asset_ids": ["asset:retained"],
            "proof_record_ids": ["record:retained-proof"],
        }
    }
    link = create_runtime_record(
        "task.handed_off",
        {
            "actor_id": "plugin:task.handoff.operator.v1",
            "allowance_returns": [returned],
            "budget": 3,
            "candidate_id": "candidate:handoff",
            "cancel_record_ids": ["record:cancel"],
            "cancelled_contract_ids": ["task:donor"],
            "completed_outputs": ["draft"],
            "contract_id": source.id,
            "hold_id": "",
            "key_id": "plugin-key",
            "preserved_contract_ids": [],
            "reason": "Continue the unfinished report.",
            "repair_contract_ids": ["task:repair-child"],
            "repair_output_proofs": repair,
            "repair_record_ids": ["record:repair-terminal"],
            "retained_asset_ids": ["asset:retained"],
            "retained_output_proofs": retained,
            "return_record_ids": ["record:return"],
            "returned_allowance": 2,
            "source_original_balance": 0,
            "successor_id": "task:successor",
        },
    )
    store = SQLiteStore(database)
    store.append_runtime_record(link)
    store.close()

    history = _invoke_json(CliRunner(), ["task", "handoffs", source.id, "--json"])
    item = history["data"][0]
    assert item["record_id"] == link.id
    assert item["allowance_returns"] == [returned]
    assert isinstance(item["allowance_returns"][0], dict)
    assert item["repair_output_proofs"] == repair
    assert isinstance(item["repair_output_proofs"], dict)
    assert isinstance(item["repair_output_proofs"]["task:repair-child"][0], dict)
    assert item["retained_output_proofs"] == retained
    assert isinstance(item["retained_output_proofs"]["draft"], dict)


def test_null_budget_replay_after_source_terminal_is_stable_and_changed_input_rejects(
    tmp_path, monkeypatch
):
    database, _root_key, source = _workspace(tmp_path, monkeypatch)
    assert (
        task_handoff_effect(source.id, "Retry safely", budget=None).payload["budget"]
        is None
    )
    runner = CliRunner()
    args = [
        "task",
        "handoff",
        source.id,
        "--reason",
        "Retry safely",
        "--idempotency-key",
        "stable-handoff",
        "--json",
    ]
    original = _invoke_json(runner, args)["data"]
    replay = _invoke_json(runner, args)["data"]
    assert replay == original

    changed = runner.invoke(
        _app(),
        [
            "task",
            "handoff",
            source.id,
            "--reason",
            "Different work",
            "--idempotency-key",
            "stable-handoff",
            "--json",
        ],
    )
    assert changed.exit_code == 1
    assert json.loads(changed.output)["ok"] is False
    store = SQLiteStore.snapshot_from_path(database)
    links = store.scan_runtime_records(record_type="task.handed_off")
    successors = [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == source.id
    ]
    store.close()
    assert len(links) == 1
    assert len(successors) == 1


def test_invalid_handoff_does_not_create_identity_or_facts(tmp_path, monkeypatch):
    database, root_key_path, source = _workspace(tmp_path, monkeypatch)
    key_path = _plugin_key_path(root_key_path)
    assert not key_path.exists()
    runner = CliRunner()
    invalid_requests = (
        ["task", "handoff", source.id, "--reason", "  ", "--json"],
        [
            "task",
            "handoff",
            source.id,
            "--reason",
            "valid",
            "--hold-reason",
            "  ",
            "--json",
        ],
        [
            "task",
            "handoff",
            source.id,
            "--reason",
            "valid",
            "--idempotency-key",
            "  ",
            "--json",
        ],
        ["task", "handoff", "missing", "--reason", "valid", "--json"],
    )
    for args in invalid_requests:
        result = runner.invoke(_app(), args)
        assert result.exit_code == 1
        assert json.loads(result.output)["ok"] is False
        assert not key_path.exists()
    store = SQLiteStore.snapshot_from_path(database)
    assert store.scan_runtime_records(record_type="task.handed_off") == []
    assert store.scan_runtime_records(record_type="candidate.received") == []
    store.close()


def test_handoff_human_output_and_optional_json_failure(tmp_path, monkeypatch):
    _database, _root_key, source = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    human = runner.invoke(
        _app(),
        [
            "task",
            "handoff",
            source.id,
            "--reason",
            "Human readable",
            "--hold-reason",
            "Need review",
        ],
    )
    assert human.exit_code == 0, human.output
    assert source.id in human.output
    assert "successor:" in human.output
    assert (
        "Need review" not in human.output
    )  # The durable link stores the handoff reason.
    assert "release with:" in human.output
    history = runner.invoke(_app(), ["task", "handoffs", source.id])
    assert history.exit_code == 0, history.output
    assert source.id in history.output
    assert "successor:" in history.output
    assert "release with:" in history.output

    bad = runner.invoke(_app(), ["task", "handoff", "missing", "--reason", "x"])
    assert bad.exit_code == 1
    assert "Error:" in bad.output
    assert "unknown Contract" in bad.output


def test_postcommit_redis_pending_reports_retained_handoff_and_replay(
    tmp_path, monkeypatch
):
    database, _root_key, source = _workspace(tmp_path, monkeypatch)
    from aigineering.cli.main import cli
    from aigineering.local_identity import ensure_local_plugin_publisher
    from aigineering.runtime_services import RedisSyncPending

    wrapped = cli.commands["task"].commands["handoff"]
    assert getattr(wrapped.callback, "_redis_protected", False)
    isolated = _app().commands["task"].commands["handoff"]
    assert not getattr(isolated.callback, "_redis_protected", False)

    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
    store = SQLiteStore(database)
    ensure_local_plugin_publisher(
        store,
        "task.handoff.operator.v1",
        ("contract.cancel", "contract.publish", "task.block", "task.handoff"),
    )
    store.close()

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr("aigineering.task_handoff.sync_runtime", pending_after_commit)
    args = [
        "task",
        "handoff",
        source.id,
        "--reason",
        "Recover durable handoff",
        "--idempotency-key",
        "pending-handoff",
        "--json",
    ]
    pending_result = CliRunner().invoke(cli, args)
    assert pending_result.exit_code == 1, pending_result.output
    pending = json.loads(pending_result.output)
    assert pending["error"] == "redis_sync_pending"
    assert pending["facts_retained"] is True
    assert pending["candidate_accepted"] is True

    monkeypatch.setattr("aigineering.task_handoff.sync_runtime", lambda *_a, **_k: None)
    replay = CliRunner().invoke(cli, args)
    assert replay.exit_code == 0, replay.output
    replay_data = json.loads(replay.output)["data"]
    store = SQLiteStore.snapshot_from_path(database)
    links = store.scan_runtime_records(record_type="task.handed_off")
    successors = [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == source.id
    ]
    store.close()
    assert len(links) == 1
    assert len(successors) == 1
    assert replay_data["record_id"] == links[0][1].id
    assert replay_data["candidate_id"] == links[0][1].payload["candidate_id"]
