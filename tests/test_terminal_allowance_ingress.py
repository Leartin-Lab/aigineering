"""Terminal allowance consequences across Candidate and internal Store writers."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from aigineering.agent.worker import WorkerExecutionError
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.ids import contract_identity_v3
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import create_entry
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_expired_claims,
)
from conftest import candidate_runtime, hosted_worker


def _records(store, record_type: str):
    return tuple(
        record for _, record in store.scan_runtime_records(record_type=record_type)
    )


def _assert_signed_claim_chain(store, proposal):
    records = tuple(record for _, record in store.scan_runtime_records())
    receipt = next(
        record
        for record in records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == proposal.id
    )
    request = next(
        record
        for record in records
        if record.record_type == "worker.claim.requested"
        and record.payload.get("candidate_id") == proposal.id
    )
    grant = next(
        record
        for record in records
        if record.record_type == "claim.granted" and request.id in record.causal_parents
    )
    assert request.causal_parents == (receipt.id,)
    assert grant.causal_parents == (request.id,)


def test_claimed_signed_worker_failure_extinguishes_remaining_allowance(
    tmp_path, monkeypatch
):
    store = SQLiteStore(str(tmp_path / "worker-failure.sqlite"))
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:terminal-worker-failure", outputs=("report",), budget=3)
    )

    class FailingWorker:
        worker_id = "worker:terminal-failure"

        def invoke(self, _contract, _disclosed_assets):
            raise WorkerExecutionError("fixture_timeout", "safe timeout")

    host = hosted_worker(
        store,
        FailingWorker(),
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claim_candidate = host.claim_candidate(
        request_id="terminal-worker-failure-claim",
        contract_id=contract.id,
        lease_seconds=60,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        lease_seconds=60,
        claim_candidate=claim_candidate,
    )
    assert claimed is not None
    _assert_signed_claim_chain(store, claim_candidate)

    stale_terminal = create_terminal_record(contract.id, "failed")
    with pytest.raises(
        sqlite3.IntegrityError, match="active worker claim predicate failed"
    ):
        store.commit_worker_invocation_failure(
            trace_entry=create_entry(contract.id, "stale_invocation_failure"),
            runtime_records=(stale_terminal,),
            claim_id=claimed.package.claim_id,
            worker_id="worker:stale",
            package_id=claimed.package.package_id,
            claim_epoch=claimed.package.claim_epoch,
        )
    assert store.get_claim(contract.id)["status"] == "active"
    assert not _records(store, "lifecycle.terminal")
    assert not _records(store, "allowance.extinguished")

    insert_record = store._insert_runtime_record

    def fail_extinguishment(record, *args, **kwargs):
        if record.record_type == "allowance.extinguished":
            raise RuntimeError("injected extinguishment write failure")
        return insert_record(record, *args, **kwargs)

    monkeypatch.setattr(store, "_insert_runtime_record", fail_extinguishment)
    with pytest.raises(RuntimeError, match="injected extinguishment write failure"):
        execute_claimed_package(claimed, host, store)
    assert store.get_claim(contract.id)["status"] == "active"
    assert not _records(store, "worker.invocation_failed")
    assert not _records(store, "lifecycle.terminal")
    assert not _records(store, "allowance.extinguished")
    monkeypatch.setattr(store, "_insert_runtime_record", insert_record)

    with pytest.raises(WorkerInvocationError, match="claim was released"):
        execute_claimed_package(claimed, host, store)

    failures = _records(store, "worker.invocation_failed")
    terminals = _records(store, "lifecycle.terminal")
    extinguishments = tuple(
        record
        for record in _records(store, "allowance.extinguished")
        if record.payload.get("contract_id") == contract.id
    )
    assert len(failures) == len(terminals) == len(extinguishments) == 1
    assert terminals[0].payload["terminal"] == "failed"
    assert terminals[0].payload["contract_id"] == contract.id
    assert extinguishments[0].payload["amount"] == 3
    assert extinguishments[0].causal_parents == (terminals[0].id,)
    assert store.get_claim(contract.id)["status"] == "released"
    assert failures[0].payload["category"] == "worker_error:fixture_timeout"
    store.close()


@pytest.mark.parametrize("store_kind", ["sqlite", "memory"])
def test_asset_free_internal_terminal_ingress_derives_and_returns_extinguishment(
    store_kind, tmp_path
):
    # This directly exercises the trusted Store ingress seam; it is not an
    # operational identity or signed Candidate proof.
    store = SQLiteStore(":memory:") if store_kind == "sqlite" else MemoryStore()
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id=f"task:asset-free-{store_kind}", outputs=("report",), budget=4)
    )
    terminal = create_terminal_record(contract.id, "failed")

    _traces, derived = store.commit_ingress_batch(
        accepted_assets=[], trace_entries=[], runtime_records=(terminal,)
    )

    persisted = tuple(
        record
        for record in _records(store, "allowance.extinguished")
        if record.payload.get("contract_id") == contract.id
    )
    assert len(persisted) == 1
    assert persisted[0].payload["amount"] == 4
    assert persisted[0].causal_parents == (terminal.id,)
    assert persisted[0] in derived
    if isinstance(store, SQLiteStore):
        before = store.runtime_materialization_digest()
        _traces, replay_derived = store.commit_ingress_batch(
            accepted_assets=[], trace_entries=[], runtime_records=(terminal,)
        )
        assert not replay_derived
        assert store.runtime_materialization_digest() == before
        assert store.rebuild_runtime_materializations(strict=True) == before
    if isinstance(store, SQLiteStore):
        store.close()


@pytest.mark.parametrize("store_kind", ["sqlite", "memory"])
def test_persisted_terminal_replay_does_not_mint_a_missing_historical_burn(
    store_kind, tmp_path
):
    database = str(tmp_path / "historical-terminal.sqlite")
    store = SQLiteStore(database) if store_kind == "sqlite" else MemoryStore()
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(
            id=f"task:historical-terminal-{store_kind}", outputs=("report",), budget=2
        )
    )
    terminal = create_terminal_record(contract.id, "failed")
    store.append_runtime_record(terminal)

    _traces, derived = store.commit_ingress_batch(
        accepted_assets=[], trace_entries=[], runtime_records=(terminal,)
    )

    assert not _records(store, "allowance.extinguished")
    assert not any(record.record_type == "allowance.extinguished" for record in derived)
    if isinstance(store, SQLiteStore):
        before = store.runtime_materialization_digest()
        assert store.rebuild_runtime_materializations(strict=True) == before
        store.close()
        reopened = SQLiteStore(database)
        try:
            assert reopened.rebuild_runtime_materializations(strict=True) == before
            assert not _records(reopened, "allowance.extinguished")
        finally:
            reopened.close()


def test_signed_candidate_output_control_still_extinguishes_terminal_allowance():
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:candidate-terminal-control", outputs=("report",), budget=2)
    )
    asset = Asset(
        id="asset:terminal-control",
        name="report",
        content="accepted result",
        created_by=contract.id,
        origin="worker",
        promptable=True,
    )
    decision = setup.publisher.publish(
        (asset_proposal_effect(asset),), idempotency_key="candidate-terminal-control"
    )
    assert decision.accepted
    assert any(
        record.record_type == "allowance.extinguished"
        and record.payload.get("contract_id") == contract.id
        for record in decision.runtime_records
    )
    store.close()


@pytest.mark.parametrize("store_kind", ["sqlite", "memory"])
def test_nonterminal_ingress_does_not_scan_allowance_history(store_kind, monkeypatch):
    store = SQLiteStore(":memory:") if store_kind == "sqlite" else MemoryStore()

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("non-terminal ingress scanned allowance history")

    monkeypatch.setattr(store, "scan_runtime_records", unexpected_scan)
    store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=[],
        runtime_records=(create_runtime_record("test.observed", {"ok": True}),),
    )
    if isinstance(store, SQLiteStore):
        store.close()


def test_expired_claim_extinguishes_remaining_allowance(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / "claim-expiry.sqlite"))
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:terminal-expired-claim", outputs=("report",), budget=5)
    )

    class IdleWorker:
        worker_id = "worker:terminal-expiry"

        def invoke(self, _contract, _disclosed_assets):
            raise AssertionError("expired work must not execute")

    host = hosted_worker(
        store,
        IdleWorker(),
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claim_candidate = host.claim_candidate(
        request_id="terminal-expiry-claim",
        contract_id=contract.id,
        lease_seconds=1,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        lease_seconds=1,
        claim_candidate=claim_candidate,
    )
    assert claimed is not None
    _assert_signed_claim_chain(store, claim_candidate)

    grant = next(
        record
        for record in _records(store, "claim.granted")
        if record.payload.get("claim_id") == claimed.package.claim_id
    )
    stale_expiration = create_runtime_record(
        "claim.expired",
        {
            "claim_id": claimed.package.claim_id,
            "contract_id": contract.id,
            "epoch": claimed.package.claim_epoch,
            "lease_until": claimed.package.lease_until,
            "package_id": claimed.package.package_id,
            "worker_id": host.worker_id,
        },
        causal_parents=(grant.id,),
    )
    stale_terminal = create_terminal_record(
        contract.id, "failed", causal_parents=(stale_expiration.id,)
    )
    assert not store.commit_claim_expiration(
        trace_entry=create_entry(contract.id, "stale_claim_expiration"),
        runtime_records=(stale_expiration, stale_terminal),
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
        expected_lease_until="stale-lease-fence",
        observed_at=datetime.now().isoformat(),
    )
    assert store.get_claim(contract.id)["status"] == "active"
    assert not _records(store, "lifecycle.terminal")
    assert not _records(store, "allowance.extinguished")

    from aigineering import runtime as runtime_module

    real_datetime = datetime

    class FutureDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=5)

    monkeypatch.setattr(runtime_module, "datetime", FutureDatetime)

    recovery_publishers = CandidatePublisherRegistry(
        (("recovery.publish.v1", setup.publisher),)
    )
    assert process_expired_claims(store, candidate_publishers=recovery_publishers) == [
        contract.id
    ]

    terminal = next(
        record
        for record in _records(store, "lifecycle.terminal")
        if record.payload.get("contract_id") == contract.id
    )
    extinguished = next(
        record
        for record in _records(store, "allowance.extinguished")
        if record.payload.get("contract_id") == contract.id
    )
    assert terminal.payload["terminal"] == "failed"
    assert extinguished.payload["amount"] == 5
    assert extinguished.causal_parents == (terminal.id,)
    assert store.get_claim(contract.id)["status"] == "expired"
    store.close()


def test_asset_free_internal_writer_does_not_replace_a_forged_extinguishment():
    # Supplying an invalid burn at the internal ingress boundary must continue
    # to fail the canonical H3 validator; auto-materialization cannot mask it.
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:forged-burn", outputs=("report",), budget=3)
    )
    terminal = create_terminal_record(contract.id, "failed")
    forged = create_runtime_record(
        "allowance.extinguished",
        {"amount": 1, "contract_id": contract.id, "reason": "cancelled"},
        causal_parents=(terminal.id,),
    )

    with pytest.raises(ValueError, match="exact terminal parent and reason"):
        store.commit_ingress_batch(
            accepted_assets=[],
            trace_entries=[],
            runtime_records=(terminal, forged),
        )

    assert not _records(store, "lifecycle.terminal")
    assert not _records(store, "allowance.extinguished")
    store.close()


def test_signed_new_child_cannot_reserve_from_historical_terminal_source():
    # Simulate immutable pre-B0 history with a terminal but no burn. The source
    # and attempted child declaration remain signed Candidate publications.
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    source = setup.accept_contract(
        Contract(id="task:legacy-terminal-source", outputs=("source",), budget=3)
    )
    legacy_terminal = create_terminal_record(source.id, "failed")
    store.append_runtime_record(legacy_terminal)
    child = Contract(
        id="task:post-terminal-child",
        parent_id=source.id,
        outputs=("child-result",),
        budget=1,
    )
    child = replace(child, id=contract_identity_v3(child))

    decision = setup.publisher.publish(
        (contract_declaration_effect(child),),
        idempotency_key="child-after-historical-terminal",
    )

    assert not decision.accepted
    assert (
        "terminal contract"
        in " ".join(
            str(record.payload.get("reason", "")) for record in decision.runtime_records
        ).lower()
    )
    assert store.get_contract(child.id) is None
    assert _records(store, "allowance.extinguished") == ()
    store.close()
