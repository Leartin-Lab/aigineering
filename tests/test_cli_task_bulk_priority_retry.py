from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from aigineering.cli._task_batch import collect_task_batch, target_key
from aigineering.cli.main import cli
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.domain import load_genesis
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.local_identity import ensure_local_domain
from aigineering.local_identity import load_actor_signer
from aigineering.task_publication import publish_task_contract
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_priority import publish_task_priority as publish_priority_owner
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.runtime import claim_next_package
from conftest import hosted_worker


class _Collaboration:
    def sync(self):
        return None


class _ClaimOnlyWorker:
    worker_id = "worker:bulk-retry-claim-preservation"

    def registration(self):
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        raise AssertionError("retry test must not invoke a Worker")


def _workspace(tmp_path: Path, monkeypatch, count: int = 3):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(database.parent / "identity/root.ed25519")
    )
    for module in (
        "aigineering.local_publication",
        "aigineering.task_priority",
        "aigineering.task_publication",
        "aigineering.application",
        "aigineering.cli.infrastructure",
        "aigineering.cli.task_priority",
        "aigineering.cli.task_board",
        "aigineering.cli._task_batch",
    ):
        try:
            monkeypatch.setattr(f"{module}.require_redis", lambda: None)
        except AttributeError:
            pass
    for module in (
        "aigineering.task_priority",
        "aigineering.task_publication",
        "aigineering.local_publication",
    ):
        try:
            monkeypatch.setattr(f"{module}.sync_runtime", lambda *_a, **_kw: None)
        except AttributeError:
            pass
    store = SQLiteStore(database)
    ensure_local_domain(store)
    # P0-7: bulk retry targets must be funded by a containment parent so the
    # shared projection reserves their allowance instead of minting root grants.
    parent = build_control_plane_contract(
        name="bulk-retry-funder",
        description="Funded containment parent for bulk retry conservation.",
        inputs=(),
        outputs=(),
        # Each child publication reserves 3; a retry of each child reserves 3
        # again, so the parent must fund twice the total child budget.
        budget=count * 6,
        activation="",
        tool_scope=(),
        labels=(),
    )
    publish_task_contract(store, parent)
    contracts = []
    for index in range(count):
        contract = contract_from_fields(
            name=f"bulk-task-{index}",
            description=f"bulk task {index}",
            inputs=(),
            outputs=(),
            budget=3,
            activation="",
            tool_scope=(),
            labels=(),
            origin="human",
            parent_id=parent.id,
            minting_authority=(),
        )
        publish_task_contract(store, contract)
        contracts.append(contract)
    store.close()

    def open_snapshot():
        return SQLiteStore.snapshot_from_path(database)

    def open_store():
        return SQLiteStore(database)

    monkeypatch.setattr(
        "aigineering.cli.task_priority._persistent_snapshot", open_snapshot
    )
    monkeypatch.setattr(
        "aigineering.cli.task_board._persistent_snapshot", open_snapshot
    )
    monkeypatch.setattr("aigineering.cli.task_priority._persistent_store", open_store)
    monkeypatch.setattr("aigineering.cli.task_board.persistent_store", open_store)
    for module in (
        "aigineering.task_priority",
        "aigineering.task_publication",
        "aigineering.local_publication",
        "aigineering.cli._task_batch",
        "aigineering.application",
        "aigineering.cli.task_priority",
    ):
        try:
            monkeypatch.setattr(
                f"{module}.collaboration_service", lambda _store: _Collaboration()
            )
        except AttributeError:
            pass
    for module in (
        "aigineering.task_priority",
        "aigineering.task_publication",
        "aigineering.local_publication",
    ):
        try:
            monkeypatch.setattr(f"{module}.sync_runtime", lambda *_a, **_kw: None)
        except AttributeError:
            pass
    return database, tuple(contracts)


def _invoke(args):
    return CliRunner().invoke(cli, args)


def test_bulk_priority_exact_empty_heads_replay_order_subset_and_revision(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    first, second, third = contracts
    base = "priority-batch-2026-01"
    args = [
        "task",
        "priority",
        first.id,
        second.id,
        third.id,
        "-7",
        "--reason",
        "incident response",
        "--previous-for",
        f"{first.id}=",
        "--previous-for",
        f"{second.id}=",
        "--previous-for",
        f"{third.id}=",
        "--idempotency-key",
        base,
        "--json",
    ]
    created = _invoke(args)
    assert created.exit_code == 0, created.output
    payload = json.loads(created.stdout)
    rows = payload["data"]["results"]
    assert [row["contract_id"] for row in rows] == [first.id, second.id, third.id]
    assert all(row["outcome"] == "accepted" for row in rows)
    assert [row["data"]["priority"] for row in rows] == [-7, -7, -7]
    assert len({row["idempotency_key"] for row in rows}) == 3
    assert all(type(row["revision"]) is int for row in rows)

    advanced = _invoke(
        [
            "task",
            "priority",
            first.id,
            second.id,
            third.id,
            "0",
            "--previous-for",
            f"{first.id}={rows[0]['data']['record_id']}",
            "--previous-for",
            f"{second.id}={rows[1]['data']['record_id']}",
            "--previous-for",
            f"{third.id}={rows[2]['data']['record_id']}",
            "--reason",
            "clear explicit priority hints",
            "--json",
        ]
    )
    assert advanced.exit_code == 0, advanced.output
    clear_rows = json.loads(advanced.stdout)["data"]["results"]
    assert all(row["data"]["priority"] == 0 for row in clear_rows)

    reversed_replay = _invoke(
        [
            "task",
            "priority",
            second.id,
            first.id,
            third.id,
            "-7",
            "--reason",
            "incident response",
            "--previous-for",
            f"{first.id}=",
            "--previous-for",
            f"{second.id}=",
            "--previous-for",
            f"{third.id}=",
            "--idempotency-key",
            base,
            "--json",
        ]
    )
    assert reversed_replay.exit_code == 0, reversed_replay.output
    replay_rows = json.loads(reversed_replay.stdout)["data"]["results"]
    replay_by_id = {row["contract_id"]: row for row in replay_rows}
    for row in rows:
        replay = replay_by_id[row["contract_id"]]
        assert replay["candidate_id"] == row["candidate_id"]
        assert replay["fact_ids"] == row["fact_ids"]
        assert replay["idempotency_key"] == row["idempotency_key"]

    subset = _invoke(
        [
            "task",
            "priority",
            third.id,
            second.id,
            "-7",
            "--reason",
            "incident response",
            "--previous-for",
            f"{second.id}=",
            "--previous-for",
            f"{third.id}=",
            "--idempotency-key",
            base,
            "--json",
        ]
    )
    assert subset.exit_code == 0, subset.output
    subset_rows = json.loads(subset.stdout)["data"]["results"]
    subset_by_id = {row["contract_id"]: row for row in subset_rows}
    assert (
        subset_by_id[second.id]["candidate_id"]
        == replay_by_id[second.id]["candidate_id"]
    )
    assert (
        subset_by_id[third.id]["candidate_id"] == replay_by_id[third.id]["candidate_id"]
    )

    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert (
            len(snapshot.scan_runtime_records(record_type="task.priority.changed")) == 6
        )
        assert snapshot.get_contract(first.id) is not None
    finally:
        snapshot.close()


def test_bulk_priority_stale_head_is_typed_rejection_and_later_target_runs(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, second = contracts
    seeded = _invoke(
        ["task", "priority", first.id, "5", "--reason", "seed head", "--json"]
    )
    assert seeded.exit_code == 0, seeded.output
    first_head = json.loads(seeded.stdout)["data"]["record_id"]

    result = _invoke(
        [
            "task",
            "priority",
            first.id,
            second.id,
            "8",
            "--reason",
            "stale map",
            "--previous-for",
            f"{first.id}=",
            "--previous-for",
            f"{second.id}=",
            "--idempotency-key",
            "stale-head-base",
            "--json",
        ]
    )
    assert result.exit_code == 1, result.output
    rows = json.loads(result.stdout)["data"]["results"]
    rejected, accepted = rows
    assert rejected["contract_id"] == first.id
    assert rejected["outcome"] == "rejected", json.dumps(rejected)
    assert rejected["error"]["code"] == "rejected"
    assert rejected["idempotency_key"]
    assert rejected["candidate_accepted"] is None
    assert rejected["command_attempted"] is False
    assert type(rejected["revision"]) is int
    assert accepted["contract_id"] == second.id
    assert accepted["outcome"] == "accepted"
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        facts = snapshot.scan_runtime_records(record_type="task.priority.changed")
        assert (
            sum(record.payload["contract_id"] == first.id for _, record in facts) == 1
        )
        assert (
            sum(record.payload["contract_id"] == second.id for _, record in facts) == 1
        )
        assert snapshot.get_runtime_record(first_head) is not None
    finally:
        snapshot.close()


def test_bulk_retry_preserves_actual_owner_keys_and_not_found_has_no_key(
    tmp_path, monkeypatch
):
    _database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, second = contracts
    result = _invoke(["task", "retry", first.id, second.id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    rows = payload["data"]["results"]
    assert [row["contract_id"] for row in rows] == [first.id, second.id]
    assert all(row["outcome"] == "accepted" for row in rows)
    for row in rows:
        data = row["data"]
        assert data["original_contract_id"] == row["contract_id"]
        assert data["idempotency_key"] == (
            f"retry:{data['original_contract_id']}:{data['retry_contract_id']}"
        )
        assert row["idempotency_key"] == data["idempotency_key"]
        assert row["fact_ids"] == [data["record_id"]]
        assert type(row["revision"]) is int
        assert data["record_revision"] <= row["revision"]

    replay = _invoke(["task", "retry", second.id, first.id, "--json"])
    assert replay.exit_code == 0, replay.output
    replay_rows = json.loads(replay.stdout)["data"]["results"]
    replay_by_id = {row["contract_id"]: row for row in replay_rows}
    for row in rows:
        assert replay_by_id[row["contract_id"]]["candidate_id"] == row["candidate_id"]
        assert replay_by_id[row["contract_id"]]["fact_ids"] == row["fact_ids"]
        assert (
            replay_by_id[row["contract_id"]]["idempotency_key"]
            == row["idempotency_key"]
        )

    mixed = _invoke(["task", "retry", "task:v6:missing", first.id, "--json"])
    assert mixed.exit_code == 1, mixed.output
    mixed_rows = json.loads(mixed.stdout)["data"]["results"]
    assert mixed_rows[0]["outcome"] == "not_found"
    assert "idempotency_key" not in mixed_rows[0]
    assert mixed_rows[1]["outcome"] == "accepted"


def test_bulk_retry_keeps_live_original_claim_and_causal_history(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    original = contracts[0]
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda *_a, **_kw: None
    )
    store = SQLiteStore(database)
    genesis = load_genesis(store)
    host = hosted_worker(
        store,
        _ClaimOnlyWorker(),
        genesis=genesis,
        authority_key=genesis.root_keys[0],
        authority_signer=load_actor_signer(),
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=original.id,
        expected_registration_version=host.registration_version,
        claim_candidate=host.claim_candidate(
            request_id="bulk-retry-original-active-claim",
            contract_id=original.id,
        ),
    )
    assert claimed is not None
    claim_history = tuple(
        (revision, record.id, record.record_type, record.payload)
        for revision, record in store.scan_runtime_records()
        if record.payload.get("contract_id") == original.id
        and record.record_type.startswith("claim.")
    )
    store.close()

    result = _invoke(["task", "retry", original.id, contracts[1].id, "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)["data"]["results"]
    assert [row["outcome"] for row in rows] == ["accepted", "accepted"]
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        after = tuple(
            (revision, record.id, record.record_type, record.payload)
            for revision, record in snapshot.scan_runtime_records()
            if record.payload.get("contract_id") == original.id
            and record.record_type.startswith("claim.")
        )
        assert after == claim_history
        assert snapshot.get_claim(original.id) is not None
        assert all(row["data"]["retry_contract_id"] != original.id for row in rows)
    finally:
        snapshot.close()


def test_bulk_retry_pending_keeps_exact_successor_and_replays_same_result(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, second = contracts

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", pending_after_commit
    )
    args = ["task", "retry", first.id, second.id, "--json"]
    pending = _invoke(args)
    assert pending.exit_code == 1, pending.output
    rows = json.loads(pending.stdout)["data"]["results"]
    assert [row["outcome"] for row in rows] == [
        "redis_sync_pending",
        "redis_sync_pending",
    ]
    for row in rows:
        data = row["data"]
        assert row["candidate_accepted"] is True
        assert row["facts_retained"] is True
        assert row["candidate_id"] == data["candidate_id"]
        assert row["idempotency_key"] == data["idempotency_key"]
        assert row["fact_ids"] == [data["record_id"]]
        assert type(row["revision"]) is int

    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_kw: None
    )
    replay = _invoke(args)
    assert replay.exit_code == 0, replay.output
    replay_rows = json.loads(replay.stdout)["data"]["results"]
    replay_by_id = {row["contract_id"]: row for row in replay_rows}
    for row in rows:
        current = replay_by_id[row["contract_id"]]
        assert current["candidate_id"] == row["candidate_id"]
        assert current["fact_ids"] == row["fact_ids"]
        assert current["idempotency_key"] == row["idempotency_key"]
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        # 2 originals + 2 retry successors + 1 containment funder parent.
        assert len(snapshot.scan_runtime_records(record_type="contract.declared")) == 5
        for original in (first, second):
            assert snapshot.get_contract(original.id).id == original.id
    finally:
        snapshot.close()


def test_bulk_retry_retains_root_rejection_and_continues_funded_target(
    tmp_path, monkeypatch
):
    database, (child,) = _workspace(tmp_path, monkeypatch, count=1)
    result = _invoke(["task", "retry", child.parent_id, child.id, "--json"])
    assert result.exit_code == 1, result.output
    rows = json.loads(result.stdout)["data"]["results"]
    assert len(rows) == 2
    by_id = {row["contract_id"]: row for row in rows}
    rejected = by_id[child.parent_id]
    accepted = by_id[child.id]
    assert rejected["candidate_accepted"] is False
    assert rejected["facts_retained"] is True
    assert rejected["fact_ids"] == [rejected["data"]["rejection_fact_id"]]
    assert accepted["outcome"] == "accepted"
    with SQLiteStore.snapshot_from_path(database) as store:
        assert store.get_contract(accepted["data"]["retry_contract_id"]) is not None
        assert len(store.scan_runtime_records(record_type="allowance.granted")) == 3
        assert len(store.scan_runtime_records(record_type="allowance.reserved")) == 2


def test_bulk_priority_accepts_exact_postcommit_pending_then_replays_same_facts(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, second = contracts

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr("aigineering.task_priority.sync_runtime", pending_after_commit)
    args = [
        "task",
        "priority",
        first.id,
        second.id,
        "6",
        "--reason",
        "pending replay",
        "--previous-for",
        f"{first.id}=",
        "--previous-for",
        f"{second.id}=",
        "--idempotency-key",
        "pending-base",
        "--json",
    ]
    pending = _invoke(args)
    assert pending.exit_code == 1, pending.output
    pending_rows = json.loads(pending.stdout)["data"]["results"]
    assert [row["outcome"] for row in pending_rows] == [
        "redis_sync_pending",
        "redis_sync_pending",
    ]
    for row in pending_rows:
        assert row["candidate_accepted"] is True
        assert row["facts_retained"] is True
        assert row["data"]["idempotency_key"] == row["idempotency_key"]
        assert row["fact_ids"] == [row["data"]["record_id"]]
        assert type(row["revision"]) is int
        assert row["candidate_id"]

    monkeypatch.setattr(
        "aigineering.task_priority.sync_runtime", lambda *_a, **_kw: None
    )
    replay = _invoke(args)
    assert replay.exit_code == 0, replay.output
    replay_rows = json.loads(replay.stdout)["data"]["results"]
    replay_by_id = {row["contract_id"]: row for row in replay_rows}
    for row in pending_rows:
        actual = replay_by_id[row["contract_id"]]
        assert actual["candidate_id"] == row["candidate_id"]
        assert actual["fact_ids"] == row["fact_ids"]
        assert actual["idempotency_key"] == row["idempotency_key"]
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert (
            len(snapshot.scan_runtime_records(record_type="task.priority.changed")) == 2
        )
    finally:
        snapshot.close()


def test_bulk_priority_pending_missing_and_accepted_replays_exact_facts(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, last = contracts
    missing = "task:v6:priority-middle-target-does-not-exist"
    base = "priority-pending-missing-accepted"
    args = [
        "task",
        "priority",
        first.id,
        missing,
        last.id,
        "17",
        "--reason",
        "preserve pending around absent target",
        "--previous-for",
        f"{first.id}=",
        "--previous-for",
        f"{missing}=",
        "--previous-for",
        f"{last.id}=",
        "--idempotency-key",
        base,
        "--json",
    ]
    owner = __import__("aigineering.task_priority", fromlist=["sync_runtime"])
    original_sync = owner.sync_runtime
    sync_calls = {"pending_raised": False}

    def one_postcommit_pending(store, *, accepted=None):
        if accepted is True and not sync_calls["pending_raised"]:
            sync_calls["pending_raised"] = True
            raise RedisSyncPending(accepted=True)
        return original_sync(store, accepted=accepted)

    monkeypatch.setattr(
        "aigineering.task_priority.sync_runtime", one_postcommit_pending
    )
    pending_result = _invoke(args)
    assert pending_result.exit_code == 1, pending_result.output
    pending_rows = json.loads(pending_result.stdout)["data"]["results"]
    assert [row["outcome"] for row in pending_rows] == [
        "redis_sync_pending",
        "not_found",
        "accepted",
    ]
    pending, absent, accepted = pending_rows
    assert pending["candidate_accepted"] is True
    assert pending["facts_retained"] is True
    assert pending["fact_ids"] == [pending["data"]["record_id"]]
    assert pending["candidate_id"] == pending["data"]["candidate_id"]
    assert pending["idempotency_key"] == pending["data"]["idempotency_key"]
    assert type(pending["revision"]) is int
    assert pending["data"]["previous_priority_id"] == ""
    assert absent["contract_id"] == missing
    assert absent["idempotency_key"] == target_key("priority", base, missing)
    assert accepted["contract_id"] == last.id
    assert accepted["data"]["previous_priority_id"] == ""
    assert accepted["fact_ids"] == [accepted["data"]["record_id"]]
    assert accepted["idempotency_key"] == accepted["data"]["idempotency_key"]
    assert type(accepted["revision"]) is int

    monkeypatch.setattr(
        "aigineering.task_priority.sync_runtime", lambda *_a, **_k: None
    )
    replay_result = _invoke(args)
    assert replay_result.exit_code == 1, replay_result.output
    replay_rows = json.loads(replay_result.stdout)["data"]["results"]
    assert [row["outcome"] for row in replay_rows] == [
        "accepted",
        "not_found",
        "accepted",
    ]
    for before, after in ((pending, replay_rows[0]), (accepted, replay_rows[2])):
        assert after["candidate_id"] == before["candidate_id"]
        assert after["fact_ids"] == before["fact_ids"]
        assert after["idempotency_key"] == before["idempotency_key"]
    assert replay_rows[1]["idempotency_key"] == absent["idempotency_key"]

    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        facts = [
            record
            for _revision, record in snapshot.scan_runtime_records(
                record_type="task.priority.changed"
            )
        ]
        assert len(facts) == 2
        assert {record.payload["contract_id"] for record in facts} == {
            first.id,
            last.id,
        }
        assert all(record.payload["contract_id"] != missing for record in facts)
        assert {record.id for record in facts} == set(
            pending["fact_ids"] + accepted["fact_ids"]
        )
    finally:
        snapshot.close()


def test_bulk_priority_corrupt_materialized_history_stays_unknown_and_stops(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, later = contracts
    initialized = _invoke(
        ["task", "priority", first.id, "4", "--reason", "seed existing head", "--json"]
    )
    assert initialized.exit_code == 0, initialized.output
    initial = json.loads(initialized.stdout)["data"]
    store = SQLiteStore(database)
    try:
        fact = store.get_runtime_record(initial["record_id"])
        assert fact is not None
        payload = deep_thaw(fact.payload)
        payload["reason"] = ""
        malformed = create_runtime_record(
            "task.priority.changed", payload, causal_parents=fact.causal_parents
        )
        store.append_runtime_record(malformed)
    finally:
        store.close()

    result = _invoke(
        [
            "task",
            "priority",
            first.id,
            later.id,
            "9",
            "--reason",
            "must not hide corrupt history",
            "--previous-for",
            f"{first.id}={initial['record_id']}",
            "--previous-for",
            f"{later.id}=",
            "--idempotency-key",
            "corrupt-history-stop",
            "--json",
        ]
    )
    assert result.exit_code == 1, result.output
    rows = json.loads(result.stdout)["data"]["results"]
    assert [row["outcome"] for row in rows] == ["outcome_unknown", "not_attempted"]
    assert rows[0]["error"]["exception_class"] == "ValueError"
    assert rows[0]["command_attempted"] is True
    assert rows[1]["command_attempted"] is False
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        facts = snapshot.scan_runtime_records(record_type="task.priority.changed")
        assert len(facts) == 2  # one valid seed plus the retained malformed record
        assert all(record.payload["contract_id"] == first.id for _, record in facts)
    finally:
        snapshot.close()


def test_bulk_priority_unknown_failure_keeps_prior_result_and_stops(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    first, second, third = contracts
    original = __import__(
        "aigineering.cli.task_priority", fromlist=["publish_task_priority"]
    )
    publish = original.publish_task_priority
    calls = []

    def fail_second(store, contract_id, *args, **kwargs):
        calls.append(contract_id)
        if contract_id == second.id:
            raise KeyError("private marker must not be shown")
        return publish(store, contract_id, *args, **kwargs)

    monkeypatch.setattr(original, "publish_task_priority", fail_second)
    result = _invoke(
        [
            "task",
            "priority",
            first.id,
            second.id,
            third.id,
            "4",
            "--reason",
            "stop safely",
            "--previous-for",
            f"{first.id}=",
            "--previous-for",
            f"{second.id}=",
            "--previous-for",
            f"{third.id}=",
            "--idempotency-key",
            "unknown-base",
            "--json",
        ]
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    rows = payload["data"]["results"]
    assert calls == [first.id, second.id]
    assert [row["outcome"] for row in rows] == [
        "accepted",
        "outcome_unknown",
        "not_attempted",
    ]
    assert rows[1]["error"]["exception_class"] == "KeyError"
    assert "private marker" not in result.stdout
    assert rows[2]["idempotency_key"]
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert (
            len(snapshot.scan_runtime_records(record_type="task.priority.changed")) == 1
        )
    finally:
        snapshot.close()


def test_bulk_priority_exact_rejected_relay_pending_keeps_ids_and_continues(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, second = contracts
    import aigineering.task_priority as owner

    real_factory = owner._publisher
    interleaved = False

    class _InterleavingPublisher:
        def __init__(self, store):
            self.delegate = real_factory(store)

        def publish(self, *args, **kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                competing = SQLiteStore(database)
                try:
                    publish_priority_owner(
                        competing,
                        first.id,
                        5,
                        "concurrent winner",
                    )
                finally:
                    competing.close()
            return self.delegate.publish(*args, **kwargs)

    def publisher_factory(store):
        if interleaved:
            return real_factory(store)
        return _InterleavingPublisher(store)

    def rejected_relay_pending(_store, *, accepted=None):
        if accepted is False:
            raise RedisSyncPending(accepted=False)

    monkeypatch.setattr(owner, "_publisher", publisher_factory)
    monkeypatch.setattr(owner, "sync_runtime", rejected_relay_pending)
    result = _invoke(
        [
            "task",
            "priority",
            first.id,
            second.id,
            "6",
            "--reason",
            "competing write",
            "--previous-for",
            f"{first.id}=",
            "--previous-for",
            f"{second.id}=",
            "--idempotency-key",
            "reject-base",
            "--json",
        ]
    )
    assert result.exit_code == 1, result.output
    rows = json.loads(result.stdout)["data"]["results"]
    rejected, accepted = rows
    assert rejected["outcome"] == "rejected"
    assert rejected["candidate_accepted"] is False
    assert rejected["facts_retained"] is True
    assert rejected["relay_pending"] is True
    assert rejected["fact_ids"] == [rejected["data"]["rejection_fact_id"]]
    assert rejected["idempotency_key"] == rejected["data"]["idempotency_key"]
    assert type(rejected["revision"]) is int
    assert accepted["outcome"] == "accepted"
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        facts = snapshot.scan_runtime_records(record_type="task.priority.changed")
        assert len(facts) == 2
        assert any(
            record.payload["reason"] == "concurrent winner" for _, record in facts
        )
        assert any(record.payload["contract_id"] == second.id for _, record in facts)
    finally:
        snapshot.close()


def test_bulk_priority_global_map_errors_precede_redis_or_publication(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    first, second = contracts
    monkeypatch.setattr(
        "aigineering.cli._task_batch.require_redis",
        lambda: (_ for _ in ()).throw(AssertionError("preflight must not run")),
    )
    result = _invoke(
        [
            "task",
            "priority",
            first.id,
            second.id,
            "8",
            "--reason",
            "bad map",
            "--previous-for",
            f"{first.id}=",
            "--idempotency-key",
            "bad-map",
            "--json",
        ]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert snapshot.scan_runtime_records(record_type="task.priority.changed") == []
    finally:
        snapshot.close()


@pytest.mark.parametrize("command", ["priority", "retry"])
@pytest.mark.parametrize("shape", ["65", "duplicate"])
def test_bulk_priority_and_retry_reject_global_shape_before_redis(
    tmp_path, monkeypatch, command: str, shape: str
):
    _database, contracts = _workspace(tmp_path, monkeypatch, count=1)
    contract_id = contracts[0].id
    monkeypatch.setattr(
        "aigineering.cli.infrastructure.require_redis",
        lambda: (_ for _ in ()).throw(AssertionError("Redis preflight must not run")),
    )
    monkeypatch.setattr(
        "aigineering.cli._task_batch.require_redis",
        lambda: (_ for _ in ()).throw(AssertionError("Redis preflight must not run")),
    )
    targets = [f"task:v3:{index:064x}" for index in range(65)]
    if shape == "duplicate":
        targets = [contract_id, contract_id]
    if command == "priority":
        args = ["task", "priority", *targets, "4", "--reason", "shape check"]
    else:
        args = ["task", "retry", *targets]
    result = _invoke([*args, "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "invalid_input"


def test_batch_collector_opt_in_result_revision_optional_key_and_typed_rejection():
    class RetainedRejection(Exception):
        def __init__(self):
            self.data = {
                "rejection_fact_id": "fact:reject",
                "fact_ids": ["fact:reject"],
                "idempotency_key": "retry:actual-owner-key",
            }
            self.revision = 19

    def rejection_result(exc):
        return exc.data, exc.revision

    def invoke(contract_id, key):
        if contract_id == "missing":
            raise LookupError("unknown task")
        if contract_id == "rejected":
            raise RetainedRejection()
        return (
            {
                "record_id": "fact:accepted",
                "candidate_id": "candidate:accepted",
                "fact_ids": ["fact:accepted"],
                "idempotency_key": "real-operation-key",
            },
            23,
        )

    collection = collect_task_batch(
        ("missing", "rejected", "accepted"),
        action="retry",
        batch_key_base="batch-correlation",
        batch_field="batch_correlation_id",
        key_for_target=lambda _contract_id: None,
        invoke=invoke,
        expected_rejection_types=(RetainedRejection,),
        rejection_result=rejection_result,
        include_operation_revision=True,
    )
    rows = collection.data["results"]
    assert collection.data["batch_correlation_id"] == "batch-correlation"
    assert [row["outcome"] for row in rows] == ["not_found", "rejected", "accepted"]
    assert "idempotency_key" not in rows[0]
    assert rows[1]["fact_ids"] == ["fact:reject"]
    assert rows[1]["idempotency_key"] == "retry:actual-owner-key"
    assert rows[1]["revision"] == 19
    assert rows[2]["idempotency_key"] == "real-operation-key"
    assert rows[2]["revision"] == 23


def test_batch_collector_missing_rejection_result_stops_without_invented_ids():
    calls = []

    def invoke(contract_id, key):
        calls.append(contract_id)
        if contract_id == "first":
            raise RedisSyncPending(accepted=False, revision=12)
        return ({"record_id": "unexpected"}, 13)

    result = collect_task_batch(
        ("first", "second"),
        action="retry",
        batch_key_base="base",
        invoke=invoke,
        include_operation_revision=True,
    )
    rows = result.data["results"]
    assert calls == ["first"]
    assert [row["outcome"] for row in rows] == ["outcome_unknown", "not_attempted"]
    assert rows[0]["candidate_accepted"] is False
    assert rows[0]["facts_retained"] is None
    assert rows[0]["relay_pending"] is True
    assert rows[0]["revision"] == 12
    assert rows[0]["idempotency_key"] == target_key("retry", "base", "first")
    assert "fact_ids" not in rows[0]
    assert rows[1]["command_attempted"] is False
    assert result.unexpected is not None
