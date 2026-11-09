"""P0-7 CLI retry funding conservation.

A retry of a root Contract must never mint an unfunded root grant; a retry of
a contained Contract must keep drawing its allowance from the containment
parent reservation. The worker ``/retry`` path (``_claimed_retry_contract``)
is always parent-contained and must stay behaviorally unchanged.
"""

from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from aigineering.core.causal_allowance import allowance_available
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.commitment import CandidateCommitter
from aigineering.local_identity import ensure_local_domain
from aigineering.plugins.task_semantics import retry_contract
from aigineering.protocol.identity import contract_from_fields
from aigineering.task_publication import (
    RetryPublicationRejected,
    publish_task_contract,
    retry_local_task_result,
)


def _workspace(tmp_path: Path, monkeypatch) -> SQLiteStore:
    import aigineering.local_publication as local_publication
    import aigineering.task_publication as task_publication

    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(database.parent / "identity/root.ed25519")
    )
    monkeypatch.setattr(local_publication, "require_redis", lambda: None)
    monkeypatch.setattr(local_publication, "sync_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(task_publication, "sync_runtime", lambda *_a, **_kw: None)
    store = SQLiteStore(database)
    ensure_local_domain(store)
    return store


def _publish_funded_pair(store, *, parent_budget: int, child_budget: int):
    """Publish one funded root parent and one contained child Contract."""
    parent = publish_task_contract(
        store,
        build_control_plane_contract(
            name="conservation funder",
            description="Funded containment parent.",
            outputs=(),
            budget=parent_budget,
        ),
    )
    child = publish_task_contract(
        store,
        contract_from_fields(
            name="conservation target",
            description="Contained retry target.",
            inputs=(),
            outputs=(),
            activation="",
            budget=child_budget,
            tool_scope=(),
            labels=(),
            origin="human",
            parent_id=parent.id,
            minting_authority=(),
        ),
    )
    return parent, child


def _allowance_records(store, *, record_type, contract_id, field):
    return [
        record
        for _, record in store.scan_runtime_records(record_type=record_type)
        if record.payload.get(field) == contract_id
    ]


def test_root_retry_is_rejected_without_any_allowance_fact(tmp_path, monkeypatch):
    store = _workspace(tmp_path, monkeypatch)
    root = publish_task_contract(
        store,
        build_control_plane_contract(
            name="root retry target",
            description="Root Contract retry must not mint fresh allowance.",
            outputs=(),
            budget=3,
        ),
    )
    successor = retry_contract(root)
    revision_before = store.get_runtime_revision()

    with pytest.raises(RetryPublicationRejected) as excinfo:
        retry_local_task_result(store, root.id)
    rejection = excinfo.value.data
    assert rejection["rejection_fact_id"]
    assert rejection["idempotency_key"] == f"retry:{root.id}:{successor.id}"

    # No half-commit: no successor Contract, no grant, no reservation, and
    # the original Contract is unchanged. Only the audit rejection advanced.
    assert store.get_contract(successor.id) is None
    assert store.get_contract(root.id) == root
    assert (
        _allowance_records(
            store,
            record_type="allowance.granted",
            contract_id=successor.id,
            field="contract_id",
        )
        == []
    )
    assert (
        _allowance_records(
            store,
            record_type="allowance.reserved",
            contract_id=root.id,
            field="source_contract_id",
        )
        == []
    )
    assert store.get_runtime_revision() > revision_before
    store.close()


def test_contained_retry_funds_from_parent_reservation(tmp_path, monkeypatch):
    store = _workspace(tmp_path, monkeypatch)
    parent, child = _publish_funded_pair(store, parent_budget=5, child_budget=2)
    available_before = allowance_available(
        parent, tuple(record for _, record in store.scan_runtime_records())
    )

    data, revision = retry_local_task_result(store, child.id)
    successor_id = data["retry_contract_id"]
    assert data["budget"] == child.budget
    assert type(revision) is int

    records = tuple(record for _, record in store.scan_runtime_records())
    reservations = _allowance_records(
        store,
        record_type="allowance.reserved",
        contract_id=successor_id,
        field="child_contract_id",
    )
    grants = _allowance_records(
        store,
        record_type="allowance.granted",
        contract_id=successor_id,
        field="contract_id",
    )
    assert len(reservations) == 1
    assert len(grants) == 1
    assert reservations[0].payload["source_contract_id"] == parent.id
    assert (
        grants[0].payload["reservation_id"] == reservations[0].payload["reservation_id"]
    )
    # Conservation: total grants for the successor equal its single funded
    # budget; the parent's available allowance dropped by exactly that amount.
    assert sum(int(g.payload["amount"]) for g in grants) == child.budget
    assert allowance_available(parent, records) == available_before - child.budget
    assert store.get_contract(child.id) == child
    store.close()


def test_contained_retry_beyond_parent_balance_is_rejected(tmp_path, monkeypatch):
    store = _workspace(tmp_path, monkeypatch)
    # The child publication reserves 2 of the parent's 3, leaving 1; the retry
    # requests the child's full budget (2) again and must exceed the remainder.
    parent, child = _publish_funded_pair(store, parent_budget=3, child_budget=2)
    successor = retry_contract(child)
    revision_before = store.get_runtime_revision()

    with pytest.raises(RetryPublicationRejected):
        retry_local_task_result(store, child.id)

    assert store.get_contract(successor.id) is None
    assert (
        _allowance_records(
            store,
            record_type="allowance.granted",
            contract_id=successor.id,
            field="contract_id",
        )
        == []
    )
    # No new reservation was created for the rejected retry successor; the
    # only parent reservation is the child's own publication reservation.
    assert (
        _allowance_records(
            store,
            record_type="allowance.reserved",
            contract_id=successor.id,
            field="child_contract_id",
        )
        == []
    )
    records_after = tuple(record for _, record in store.scan_runtime_records())
    assert allowance_available(parent, records_after) == 1
    assert store.get_runtime_revision() > revision_before
    store.close()


def _records(store):
    return tuple(record for _, record in store.scan_runtime_records())


def _allowance_facts(store):
    return tuple(
        record
        for record in _records(store)
        if record.record_type.startswith("allowance.")
    )


@pytest.mark.parametrize("parent_budget", [2, 3])
def test_rejected_retry_replays_original_rejection_after_other_writes(
    tmp_path, monkeypatch, parent_budget
):
    store = _workspace(tmp_path, monkeypatch)
    parent, child = _publish_funded_pair(
        store, parent_budget=parent_budget, child_budget=2
    )
    before = _allowance_facts(store)
    with pytest.raises(RetryPublicationRejected) as first:
        retry_local_task_result(store, child.id)
    publish_task_contract(
        store, build_control_plane_contract(name="unrelated", budget=1)
    )
    with pytest.raises(RetryPublicationRejected) as replay:
        retry_local_task_result(store, child.id)
    assert replay.value.data == first.value.data
    assert store.get_contract(retry_contract(child).id) is None
    assert all(record in _allowance_facts(store) for record in before)
    assert len(store.scan_runtime_records(record_type="candidate.rejected")) == 1
    assert allowance_available(parent, _records(store)) == parent_budget - child.budget
    store.close()


def test_exact_retry_replay_at_zero_balance_after_terminal_reopen_and_rebuild(
    tmp_path, monkeypatch
):
    from aigineering.task_publication import publish_task_cancel

    store = _workspace(tmp_path, monkeypatch)
    parent, child = _publish_funded_pair(store, parent_budget=4, child_budget=2)
    data, _revision = retry_local_task_result(store, child.id)
    assert allowance_available(parent, _records(store)) == 0
    publish_task_cancel(store, parent.id, "Stop funding after accepted retry")
    publish_task_contract(
        store, build_control_plane_contract(name="later write", budget=1)
    )
    database = tmp_path / ".aig" / "store.db"
    before = _records(store)
    allowance_before = _allowance_facts(store)
    digest = store.runtime_materialization_digest()
    store.close()

    with SQLiteStore(database) as reopened:
        assert reopened.rebuild_runtime_materializations() == digest
        assert _records(reopened) == before
        replay, _current_revision = retry_local_task_result(reopened, child.id)
        assert replay == data
        assert _allowance_facts(reopened) == allowance_before
        assert _records(reopened) == before


@pytest.mark.parametrize("same_candidate", [True, False])
def test_sqlite_concurrent_retries_commit_only_funded_grants(
    tmp_path, monkeypatch, same_candidate
):
    store = _workspace(tmp_path, monkeypatch)
    # Two sources consume 4; only one additional budget of 2 remains.
    parent, child = _publish_funded_pair(store, parent_budget=6, child_budget=2)
    sibling = publish_task_contract(
        store,
        contract_from_fields(
            name="competing target",
            description="Competes for the same remaining allowance.",
            inputs=(),
            outputs=(),
            activation="",
            tool_scope=(),
            labels=(),
            budget=2,
            parent_id=parent.id,
            origin="human",
        ),
    )
    database = tmp_path / ".aig" / "store.db"
    store.close()
    barrier = Barrier(2)
    commit_candidate = CandidateCommitter.commit

    def synchronized_candidate_commit(committer, candidate, *args, **kwargs):
        if candidate.idempotency_key.startswith("retry:"):
            # Both signed retry Candidates reach the commitment boundary before
            # either can take the SQLite writer lock and spend the shared balance.
            barrier.wait(timeout=10)
        return commit_candidate(committer, candidate, *args, **kwargs)

    monkeypatch.setattr(CandidateCommitter, "commit", synchronized_candidate_commit)

    def publish(source):
        with SQLiteStore(database) as connection:
            try:
                data, _revision = retry_local_task_result(connection, source.id)
                return True, data
            except RetryPublicationRejected as exc:
                return False, exc.data

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(publish, source)
            for source in (child, child if same_candidate else sibling)
        ]
        results = [future.result() for future in futures]
    with SQLiteStore(database) as reopened:
        assert sum(accepted for accepted, _ in results) == (2 if same_candidate else 1)
        if same_candidate:
            assert results[0][1] == results[1][1]
        assert len(reopened.scan_runtime_records(record_type="allowance.granted")) == 4
        assert len(reopened.scan_runtime_records(record_type="allowance.reserved")) == 3
        assert allowance_available(parent, _records(reopened)) == 0
        rejected = reopened.scan_runtime_records(record_type="candidate.rejected")
        assert len(rejected) == (0 if same_candidate else 1)
        if not same_candidate:
            missing = [
                c
                for c in (child, sibling)
                if reopened.get_contract(retry_contract(c).id) is None
            ]
            assert len(missing) == 1
        # All committed prefixes preserve the root injection. Reservations move
        # rights between accounts; they do not count as a second injection.
        balances = {}
        for record in _records(reopened):
            payload = record.payload
            if record.record_type == "allowance.granted":
                balances[payload["contract_id"]] = payload["amount"]
            elif record.record_type == "allowance.reserved":
                balances[payload["source_contract_id"]] -= payload["amount"]
            elif record.record_type == "candidate.committed":
                assert all(amount >= 0 for amount in balances.values())
                assert sum(balances.values()) == parent.budget
