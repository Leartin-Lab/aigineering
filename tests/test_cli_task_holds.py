"""Signed CLI acceptance for task hold placement and release history."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner
import pytest

from aigineering.cli.task_collaboration import register_task_collaboration_commands
from aigineering.core.ids import compute_content_hash, hash_contract
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.types import Contract
from aigineering.task_views import project_task_status
from aigineering.task_collaboration import publish_task_block


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_collaboration_commands(task)
    root.add_command(task)
    return root


def _contract(name: str) -> Contract:
    return Contract(
        id=hash_contract(
            name=name,
            description="",
            inputs=[],
            outputs=["result"],
            activation="",
            budget=3,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name=name,
        outputs=("result",),
        budget=3,
    )


def _workspace(tmp_path: Path, monkeypatch, *, names=("target",)):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    store = SQLiteStore(database)
    ensure_local_domain(store)
    contracts = tuple(_contract(name) for name in names)
    for contract in contracts:
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
    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
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
    return database, root_key_path, contracts


def _holds_plugin_key_path(root_key_path: Path) -> Path:
    actor_id = "plugin:task.collaboration.holds.operator.v1"
    return root_key_path.parent / (
        f"plugin-{compute_content_hash(actor_id)[:16]}.ed25519"
    )


def _invoke(runner, args):
    result = runner.invoke(_app(), args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def test_multiple_holds_release_one_at_a_time_and_report_all_history(
    tmp_path, monkeypatch
):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    store = SQLiteStore.snapshot_from_path(database)
    initial_status = project_task_status(contract, store)
    store.close()

    first = _invoke(
        runner,
        [
            "task",
            "block",
            contract.id,
            "--reason",
            "Need source validation",
            "--idempotency-key",
            "hold-1",
            "--json",
        ],
    )["data"]
    second = _invoke(
        runner,
        [
            "task",
            "block",
            contract.id,
            "--reason",
            "Wait for reviewer",
            "--idempotency-key",
            "hold-2",
            "--json",
        ],
    )["data"]
    assert first["hold_id"] == first["record_id"]
    assert second["hold_id"] == second["record_id"]
    assert first["actor_id"] == "plugin:task.collaboration.holds.operator.v1"
    assert first["key_id"].startswith("plugin-")
    assert first["candidate_id"] and first["recorded_at"] and first["revision"]

    blocked_store = SQLiteStore.snapshot_from_path(database)
    assert project_task_status(contract, blocked_store)["status"] == "blocked"
    blocked_store.close()

    released = _invoke(
        runner,
        [
            "task",
            "unblock",
            contract.id,
            "--hold",
            first["hold_id"],
            "--reason",
            "Source validated",
            "--idempotency-key",
            "release-1",
            "--json",
        ],
    )["data"]
    replay = _invoke(
        runner,
        [
            "task",
            "unblock",
            contract.id,
            "--hold",
            first["hold_id"],
            "--reason",
            "Source validated",
            "--idempotency-key",
            "release-1",
            "--json",
        ],
    )["data"]
    assert replay == released

    holds = _invoke(runner, ["task", "holds", contract.id, "--json"])["data"]
    by_id = {item["hold_id"]: item for item in holds}
    assert by_id[first["hold_id"]]["active"] is False
    assert (
        by_id[first["hold_id"]]["release_refs"][0]["record_id"] == released["record_id"]
    )
    assert by_id[second["hold_id"]]["active"] is True
    still_blocked = SQLiteStore.snapshot_from_path(database)
    assert project_task_status(contract, still_blocked)["status"] == "blocked"
    still_blocked.close()

    _invoke(
        runner,
        [
            "task",
            "unblock",
            contract.id,
            "--hold",
            second["hold_id"],
            "--reason",
            "Reviewer ready",
            "--json",
        ],
    )
    unblocked_store = SQLiteStore.snapshot_from_path(database)
    assert project_task_status(contract, unblocked_store) == initial_status
    unblocked_store.close()
    history = _invoke(runner, ["task", "holds", contract.id, "--json"])["data"]
    assert len(history) == 2
    assert all(item["active"] is False for item in history)


def test_terminal_task_rejects_new_hold_but_allows_existing_hold_cleanup(
    tmp_path, monkeypatch
):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    placed = _invoke(
        runner,
        ["task", "block", contract.id, "--reason", "Pause", "--json"],
    )["data"]
    store = SQLiteStore(database)
    store.append_runtime_record(
        create_terminal_record(
            contract.id, "complete", actor_id="test:fixture", reason="completed"
        )
    )
    store.close()

    rejected = runner.invoke(
        _app(),
        ["task", "block", contract.id, "--reason", "Late block", "--json"],
    )
    assert rejected.exit_code == 1
    assert json.loads(rejected.output)["error"]["code"] == "rejected"

    released = _invoke(
        runner,
        [
            "task",
            "unblock",
            contract.id,
            "--hold",
            placed["hold_id"],
            "--reason",
            "Cleanup after terminal",
            "--idempotency-key",
            "terminal-cleanup",
            "--json",
        ],
    )["data"]
    replay = _invoke(
        runner,
        [
            "task",
            "unblock",
            contract.id,
            "--hold",
            placed["hold_id"],
            "--reason",
            "Cleanup after terminal",
            "--idempotency-key",
            "terminal-cleanup",
            "--json",
        ],
    )["data"]
    assert replay == released
    holds = _invoke(runner, ["task", "holds", contract.id, "--json"])["data"]
    assert holds[0]["active"] is False
    assert holds[0]["release_refs"][0]["record_id"] == released["record_id"]
    terminal_store = SQLiteStore.snapshot_from_path(database)
    assert project_task_status(contract, terminal_store)["status"] == "completed"
    terminal_store.close()


def test_invalid_and_unknown_hold_inputs_fail_before_hold_identity_or_candidate(
    tmp_path, monkeypatch
):
    database, root_key_path, (first_contract, second_contract) = _workspace(
        tmp_path, monkeypatch, names=("first", "second")
    )
    runner = CliRunner()
    key_path = _holds_plugin_key_path(root_key_path)
    assert not key_path.exists()
    baseline = SQLiteStore.snapshot_from_path(database)
    baseline_received = len(
        baseline.scan_runtime_records(record_type="candidate.received")
    )
    baseline_placements = len(
        baseline.scan_runtime_records(record_type="claim.hold.placed")
    )
    baseline.close()

    for args in (
        ["task", "block", first_contract.id, "--reason", "  ", "--json"],
        ["task", "block", "missing", "--reason", "reason", "--json"],
        ["task", "block", first_contract.id, "--reason", "x" * 65537, "--json"],
    ):
        result = runner.invoke(_app(), args)
        assert result.exit_code == 1
        assert json.loads(result.output)["ok"] is False
        assert not key_path.exists()
        snapshot = SQLiteStore.snapshot_from_path(database)
        assert (
            len(snapshot.scan_runtime_records(record_type="candidate.received"))
            == baseline_received
        )
        assert (
            len(snapshot.scan_runtime_records(record_type="claim.hold.placed"))
            == baseline_placements
        )
        snapshot.close()

    placed = _invoke(
        runner,
        ["task", "block", first_contract.id, "--reason", "Valid", "--json"],
    )["data"]
    before = SQLiteStore.snapshot_from_path(database)
    received_before = len(before.scan_runtime_records(record_type="candidate.received"))
    released_before = len(
        before.scan_runtime_records(record_type="claim.hold.released")
    )
    before.close()

    cross_task = runner.invoke(
        _app(),
        [
            "task",
            "unblock",
            second_contract.id,
            "--hold",
            placed["hold_id"],
            "--reason",
            "Wrong task",
            "--json",
        ],
    )
    assert cross_task.exit_code == 1
    assert json.loads(cross_task.output)["error"]["code"] == "not_found"
    after = SQLiteStore.snapshot_from_path(database)
    assert (
        len(after.scan_runtime_records(record_type="candidate.received"))
        == received_before
    )
    assert (
        len(after.scan_runtime_records(record_type="claim.hold.released"))
        == released_before
    )
    after.close()

    blank_key = runner.invoke(
        _app(),
        [
            "task",
            "unblock",
            first_contract.id,
            "--hold",
            placed["hold_id"],
            "--reason",
            "Reason",
            "--idempotency-key",
            "   ",
            "--json",
        ],
    )
    assert blank_key.exit_code == 1
    assert json.loads(blank_key.output)["error"]["code"] == "invalid_input"


def test_blank_direct_service_key_does_not_create_hold_actor_authorization(
    tmp_path, monkeypatch
):
    database, root_key_path, (contract,) = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    authorizations = store.scan_runtime_records(record_type="actor.authorized")
    with pytest.raises(ValueError, match="non-blank"):
        publish_task_block(store, contract.id, "Valid reason", idempotency_key=" ")
    assert store.scan_runtime_records(record_type="actor.authorized") == authorizations
    assert not _holds_plugin_key_path(root_key_path).exists()
    store.close()


def test_postcommit_redis_pending_and_replay_return_original_hold_fact(
    tmp_path, monkeypatch
):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    from aigineering.cli.main import cli
    from aigineering.runtime_services import RedisSyncPending

    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
    sync_calls = 0

    def fail_after_hold(_store, *, accepted=None):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise RedisSyncPending(accepted=accepted)

    monkeypatch.setattr("aigineering.task_collaboration.sync_runtime", fail_after_hold)
    args = [
        "task",
        "block",
        contract.id,
        "--reason",
        "Durable hold",
        "--idempotency-key",
        "stable-hold",
        "--json",
    ]
    failed = CliRunner().invoke(cli, args)
    assert failed.exit_code == 1, failed.output
    pending = json.loads(failed.output)
    assert pending["error"] == "redis_sync_pending"
    assert pending["facts_retained"] is True
    assert pending["candidate_accepted"] is True

    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", lambda *_a, **_k: None
    )
    replay = CliRunner().invoke(cli, args)
    assert replay.exit_code == 0, replay.output
    replay_data = json.loads(replay.output)["data"]
    store = SQLiteStore.snapshot_from_path(database)
    placements = store.scan_runtime_records(record_type="claim.hold.placed")
    store.close()
    assert len(placements) == 1
    original = placements[0][1]
    assert replay_data["record_id"] == original.id
    assert replay_data["hold_id"] == original.id
    assert replay_data["recorded_at"] == original.recorded_at
    assert replay_data["candidate_id"] == original.payload["candidate_id"]
    assert replay_data["revision"] == placements[0][0]


def test_human_output_keeps_copyable_ids_and_hold_history(tmp_path, monkeypatch):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()

    comment = runner.invoke(_app(), ["task", "comment", contract.id, "Readable note"])
    assert comment.exit_code == 0, comment.output
    comment_store = SQLiteStore.snapshot_from_path(database)
    comment_record = comment_store.scan_runtime_records(record_type="task.commented")[
        0
    ][1]
    comment_store.close()
    assert comment_record.id in comment.output
    assert "Readable note" in comment.output

    comments = runner.invoke(_app(), ["task", "comments", contract.id])
    assert comments.exit_code == 0, comments.output
    assert comment_record.id in comments.output
    assert "Readable note" in comments.output

    block = runner.invoke(
        _app(), ["task", "block", contract.id, "--reason", "Need review"]
    )
    assert block.exit_code == 0, block.output
    hold_store = SQLiteStore.snapshot_from_path(database)
    hold = hold_store.scan_runtime_records(record_type="claim.hold.placed")[0][1]
    hold_store.close()
    assert hold.id in block.output
    assert "Need review" in block.output

    unblock = runner.invoke(
        _app(),
        [
            "task",
            "unblock",
            contract.id,
            "--hold",
            hold.id,
            "--reason",
            "Review complete",
        ],
    )
    assert unblock.exit_code == 0, unblock.output
    release_store = SQLiteStore.snapshot_from_path(database)
    release = release_store.scan_runtime_records(record_type="claim.hold.released")[0][
        1
    ]
    release_store.close()
    assert release.id in unblock.output
    assert hold.id in unblock.output
    assert "Review complete" in unblock.output

    history = runner.invoke(_app(), ["task", "holds", contract.id])
    assert history.exit_code == 0, history.output
    assert hold.id in history.output
    assert release.id in history.output
    assert "RELEASED" in history.output
    assert "Need review" in history.output


def test_human_output_failure_is_click_error_not_json(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        _app(), ["task", "comment", "missing-contract", "Readable note"]
    )
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "unknown Contract" in result.output
    assert not result.output.lstrip().startswith("{")


@pytest.mark.parametrize("sync_fails", [False, True])
def test_claim_wins_after_frozen_projection_relays_durable_rejection(
    tmp_path, monkeypatch, sync_fails
):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    from aigineering.core.commitment import CandidateCommitRejected
    from aigineering.local_identity import ensure_local_plugin_publisher
    from aigineering.core.projection_context import load_effect_projection_context
    from aigineering.runtime_services import RedisSyncPending

    import aigineering.core.commit_snapshot as commit_snapshot
    import aigineering.core.commitment as commitment

    store = SQLiteStore(database)
    ensure_local_plugin_publisher(
        store,
        "task.collaboration.holds.operator.v1",
        ("task.block", "task.unblock"),
    )
    stale_context = load_effect_projection_context(store)
    winner = SQLiteStore(database)
    try:
        claimed = winner.claim_contract(contract.id, "worker:racing")
        assert claimed is not None
    finally:
        winner.close()
    # Keep both pure projections stale on purpose. This isolates the actual
    # SQLite BEGIN guard as the rejecting boundary after the other connection
    # wins the claim race; normal commit-snapshot revalidation is covered by
    # the handoff tests and core snapshot tests.
    projection_context_module = commitment.context_inputs
    original_loader = projection_context_module.load_candidate_projection_context
    loader_calls = []

    def frozen_context_loader(_store, candidate, **kwargs):
        if not (
            candidate.idempotency_key == "race"
            and len(candidate.effects) == 1
            and candidate.effects[0].effect_type == "task.block"
            and candidate.effects[0].payload.get("contract_id") == contract.id
        ):
            return original_loader(_store, candidate, **kwargs)
        loader_calls.append((candidate.id, stale_context))
        return stale_context

    monkeypatch.setattr(
        projection_context_module,
        "load_candidate_projection_context",
        frozen_context_loader,
    )
    original_reduce = commitment.reduce_candidate
    preprojection_decisions = []

    def observe_reduce(candidate, *args, **kwargs):
        decision = original_reduce(candidate, *args, **kwargs)
        preprojection_decisions.append((candidate.id, decision.accepted))
        return decision

    monkeypatch.setattr(commitment, "reduce_candidate", observe_reduce)
    monkeypatch.setattr(
        commit_snapshot,
        "load_effect_projection_context",
        lambda *_args, **_kwargs: stale_context,
    )
    sync_outcomes = []

    def relay(_store, *, accepted=None):
        sync_outcomes.append(accepted)
        if sync_fails and accepted is False:
            raise RedisSyncPending(accepted=False)

    monkeypatch.setattr("aigineering.task_collaboration.sync_runtime", relay)
    expected = RedisSyncPending if sync_fails else CandidateCommitRejected
    with pytest.raises(expected):
        publish_task_block(store, contract.id, "competing hold", idempotency_key="race")

    assert sync_outcomes[-1] is False
    rejected = store.scan_runtime_records(record_type="candidate.rejected")
    assert len(rejected) == 1
    candidate_id = rejected[0][1].payload.get("candidate_id")
    assert candidate_id
    assert loader_calls == [(candidate_id, stale_context)]
    assert preprojection_decisions[0] == (candidate_id, True)
    assert all(
        observed_id == candidate_id and accepted
        for observed_id, accepted in preprojection_decisions
    )
    assert (
        projection_context_module.load_candidate_projection_context
        is not original_loader
    )
    assert (
        "cannot place a hold on a Contract with claim history"
        in rejected[0][1].payload["reason"]
    )
    assert store.scan_runtime_records(record_type="claim.hold.placed") == []
    assert store.get_claim(contract.id) is not None
    store.close()


def test_bulk_block_reports_all_targets_and_exact_keyed_replay_keeps_timestamp(
    tmp_path, monkeypatch
):
    database, _root_key, (first, second) = _workspace(
        tmp_path, monkeypatch, names=("first", "second")
    )
    runner = CliRunner()
    result = runner.invoke(
        _app(),
        [
            "task",
            "block",
            first.id,
            "missing",
            second.id,
            "--reason",
            "bulk hold",
            "--idempotency-key",
            "bulk-base",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    rows = json.loads(result.output)["data"]["results"]
    assert [row["outcome"] for row in rows] == ["accepted", "not_found", "accepted"]
    assert rows[0]["idempotency_key"] != rows[2]["idempotency_key"]
    assert rows[0]["data"]["candidate_id"]
    assert rows[0]["fact_ids"] == rows[0]["data"]["fact_ids"]

    # Force replay projection timestamps away from the original second, proving
    # that durable identity excludes only recorded_at and returns the old fact.
    from aigineering.protocol import runtime_record
    from aigineering.task_collaboration import publish_task_block

    regenerated_at = "2099-01-01T00:00:00Z"
    monkeypatch.setattr(runtime_record, "now_iso", lambda: regenerated_at)
    key = rows[0]["idempotency_key"]
    store = SQLiteStore(database)
    try:
        original, _revision = publish_task_block(
            store, first.id, "bulk hold", idempotency_key=key
        )
    finally:
        store.close()
    # The original batch result already committed the same key; it is the
    # durable record whose timestamp must survive this explicit single replay.
    assert original["record_id"] == rows[0]["data"]["record_id"]
    assert original["recorded_at"] == rows[0]["data"]["recorded_at"]
    assert original["recorded_at"] != regenerated_at

    from aigineering.runtime_services import RedisSyncPending

    def pending_after_accept(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", pending_after_accept
    )
    store = SQLiteStore(database)
    try:
        with pytest.raises(RedisSyncPending) as caught:
            publish_task_block(store, first.id, "bulk hold", idempotency_key=key)
        assert caught.value.accepted is True
        assert caught.value.data["fact_ids"] == [original["record_id"]]
        assert caught.value.data["recorded_at"] == original["recorded_at"]
    finally:
        store.close()


def test_bulk_unblock_requires_one_exact_hold_per_target_and_reports_each_result(
    tmp_path, monkeypatch
):
    database, _root_key, (first, second) = _workspace(
        tmp_path, monkeypatch, names=("first", "second")
    )
    first_hold = _invoke(
        CliRunner(),
        ["task", "block", first.id, "--reason", "first hold", "--json"],
    )["data"]["hold_id"]
    second_hold = _invoke(
        CliRunner(),
        ["task", "block", second.id, "--reason", "second hold", "--json"],
    )["data"]["hold_id"]
    runner = CliRunner()
    incomplete = runner.invoke(
        _app(),
        [
            "task",
            "unblock",
            first.id,
            second.id,
            "--hold-for",
            f"{first.id}={first_hold}",
            "--reason",
            "resume",
            "--json",
        ],
    )
    assert incomplete.exit_code == 1
    assert json.loads(incomplete.output)["error"]["code"] == "invalid_input"

    result = runner.invoke(
        _app(),
        [
            "task",
            "unblock",
            first.id,
            second.id,
            "--hold-for",
            f"{first.id}={first_hold}",
            "--hold-for",
            f"{second.id}={second_hold}",
            "--reason",
            "resume",
            "--idempotency-key",
            "unblock-base",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["data"]["results"]
    assert [row["outcome"] for row in rows] == ["accepted", "accepted"]
    assert rows[0]["data"]["hold_id"] == first_hold
    assert rows[1]["data"]["hold_id"] == second_hold
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]
