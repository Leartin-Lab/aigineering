"""Authentication failures are audited without admitting claimed headers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from aigineering.agent.worker import WorkerHost
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import MemoryTraceStore, create_entry
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
    WORKER_RAW_OUTPUT_METADATA_KEY,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    claim_bound_output_effects,
    contract_cancellation_effect,
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package, process_rejected_submissions


class _NoInvokeWorker:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(
            self.worker_id, actor_id=self.worker_id, key_id="worker-key"
        )

    def invoke(self, contract, disclosed_assets):
        raise AssertionError("authentication tests must not invoke a Worker")


def _contract(name: str) -> Contract:
    fields = {
        "name": name,
        "description": "Authentication rejection regression task",
        "inputs": (),
        "outputs": (f"{name}-output",),
        "activation": "",
        "budget": 5,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _domain(path, contract_names: tuple[str, ...] = ("task-one",)):
    store = SQLiteStore(str(path))
    root_signer = Ed25519Signer()
    worker_signer = Ed25519Signer()
    root_key = ActorKey(
        "human:auth-test",
        "root-key",
        root_signer.kind,
        root_signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "asset.publish.protected",
            "contract.cancel",
            "contract.publish",
            "contract.publish.protected",
            "worker.register",
        ),
    )
    recovery_key = ActorKey(
        "plugin:recovery-test",
        "recovery-key",
        root_signer.kind,
        root_signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(
        "auth-rejection-test", (root_key, recovery_key), "policy:test"
    )
    initialize_genesis(store, genesis)
    root = CandidatePublisher(store, store, genesis, root_key, root_signer)
    worker_id = "worker:auth-test"
    worker_key = ActorKey(
        worker_id,
        "worker-key",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    registration = WorkerRegistration(
        worker_id, capacity=4, actor_id=worker_id, key_id=worker_key.key_id
    )
    decision = root.publish(
        (
            actor_authorization_effect(worker_key),
            worker_registration_effect(registration),
        ),
        idempotency_key="register-test-worker",
    )
    assert decision.accepted
    host = WorkerHost(
        _NoInvokeWorker(worker_id), genesis, worker_key, worker_signer, "1"
    )
    contracts = tuple(_contract(name) for name in contract_names)
    for index, contract in enumerate(contracts):
        declared = root.publish(
            (contract_declaration_effect(contract),),
            idempotency_key=f"declare-{index}-{contract.name}",
        )
        assert declared.accepted
    return store, root, host, contracts


def _claim(store, host: WorkerHost, contract: Contract, suffix: str = "claim"):
    claim_candidate = host.claim_candidate(
        request_id=f"{suffix}:{contract.name}",
        contract_id=contract.id,
        lease_seconds=600,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        lease_seconds=600,
        claim_candidate=claim_candidate,
    )
    assert claimed is not None
    return claimed


def _candidate(host, claimed, *, idempotency: str, effects=None, raw_output: str = ""):
    binding = CandidateClaimBinding(
        claimed.contract.id,
        claimed.package.claim_id,
        claimed.package.claim_epoch,
        claimed.package.package_id,
    )
    return create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=effects
        or (
            CandidateEffect(
                "asset.propose", {"asset": {"name": "never", "content": "never"}}
            ),
        ),
        signer=host.signer,
        idempotency_key=idempotency,
        claim_binding=binding,
        metadata=({WORKER_RAW_OUTPUT_METADATA_KEY: raw_output} if raw_output else None),
    )


def _fact_types(store: SQLiteStore) -> list[str]:
    return [record.record_type for _, record in store.scan_runtime_records()]


def test_bad_signature_with_exact_live_claim_is_audit_only_and_survives_rebuild(
    tmp_path,
):
    database = tmp_path / "auth-rejection.sqlite"
    store, _root, host, (contract,) = _domain(database)
    trace = MemoryTraceStore()
    claimed = _claim(store, host, contract)
    before_records = {record.id for _, record in store.scan_runtime_records()}
    before_claim = store.get_claim(contract.id)
    before_budget = (
        RuntimeProjection(store, store).contract_view(contract).budget_remaining
    )
    proposal = _candidate(host, claimed, idempotency="bad-exact-live-claim")
    forged = replace(proposal, signature="invalid-test-signature")

    decision = CandidateCommitter(store, trace).commit(forged)

    assert decision.accepted is False
    assert any(
        record.record_type == "candidate.authentication_rejected"
        for record in decision.runtime_records
    )
    assert len(trace.get_all()) == 1
    assert store.get_claim(contract.id) == before_claim
    assert (
        RuntimeProjection(store, store).contract_view(contract).budget_remaining
        == before_budget
    )
    assert not store.get_assets_by_name("never")
    new_records = [
        record
        for _, record in store.scan_runtime_records()
        if record.id not in before_records
    ]
    assert {record.record_type for record in new_records} <= {
        "candidate.authentication_rejected",
        "trace.recorded",
    }
    assert not store.scan_runtime_records(record_type="attempt.closed")
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    assert not store.scan_runtime_records(record_type="claim.submitted")
    assert not store.scan_runtime_records(record_type="allowance.extinguished")
    assert process_rejected_submissions(store) == []
    assert not store.scan_runtime_records(
        record_type="candidate_rejection.recovery_scheduled"
    )

    store.close()
    reopened = SQLiteStore(str(database))
    try:
        reopened.rebuild_claim_projection()
        assert reopened.get_claim(contract.id)["status"] == "active"
        assert (
            RuntimeProjection(reopened, reopened)
            .contract_view(contract)
            .budget_remaining
            == before_budget
        )
        assert (
            len(
                reopened.scan_runtime_records(
                    record_type="candidate.authentication_rejected"
                )
            )
            == 1
        )
        assert not reopened.scan_runtime_records(record_type="attempt.closed")
        assert not reopened.scan_runtime_records(record_type="lifecycle.terminal")
        assert not reopened.scan_runtime_records(record_type="claim.submitted")
    finally:
        reopened.close()


def test_bad_signature_with_untrusted_content_id_is_audit_only(tmp_path):
    store, _root, host, (contract,) = _domain(tmp_path / "untrusted-content-id.sqlite")
    claimed = _claim(store, host, contract)
    proposal = _candidate(host, claimed, idempotency="untrusted-content-id")
    forged = replace(
        proposal, id="candidate:attacker-chosen", signature="invalid-test-signature"
    )

    decision = CandidateCommitter(store, store).commit(forged)

    assert decision.accepted is False
    assert store.get_claim(contract.id)["status"] == "active"
    assert (
        len(store.scan_runtime_records(record_type="candidate.authentication_rejected"))
        == 1
    )
    assert not store.scan_runtime_records(record_type="attempt.closed")
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")


def test_repeating_same_bad_signature_does_not_apply_claim_effects_twice(tmp_path):
    store, _root, host, (contract,) = _domain(
        tmp_path / "repeated-auth-rejection.sqlite"
    )
    claimed = _claim(store, host, contract)
    proposal = replace(
        _candidate(host, claimed, idempotency="repeated-auth-rejection"),
        signature="invalid-test-signature",
    )
    committer = CandidateCommitter(store, store)

    first = committer.commit(proposal)
    second = committer.commit(proposal)

    assert first.accepted is False
    assert second.accepted is False
    assert store.get_claim(contract.id)["status"] == "active"
    assert (
        len(store.scan_runtime_records(record_type="candidate.authentication_rejected"))
        == 1
    )
    assert not store.scan_runtime_records(record_type="attempt.closed")
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")


def test_invalid_claim_headers_are_not_looked_up_or_applied(tmp_path):
    database = tmp_path / "untrusted-bindings.sqlite"
    store, _root, host, (contract_a, contract_b) = _domain(
        database, ("task-a", "task-b")
    )
    _claim(store, host, contract_a, "claim-a")
    claimed_b = _claim(store, host, contract_b, "claim-b")
    binding_variants = (
        CandidateClaimBinding(contract_a.id, "claim:does-not-exist", 1, "pkg:unknown"),
        CandidateClaimBinding(
            contract_a.id,
            claimed_b.package.claim_id,
            claimed_b.package.claim_epoch,
            claimed_b.package.package_id,
        ),
    )
    for index, binding in enumerate(binding_variants):
        proposal = create_candidate_proposal(
            domain_id=host.genesis.id,
            actor_id=host.actor_key.actor_id,
            key_id=host.actor_key.key_id,
            effects=(
                CandidateEffect(
                    "contract.cancel",
                    {"contract_id": contract_a.id, "reason": "untrusted"},
                ),
            ),
            signer=host.signer,
            idempotency_key=f"bad-binding-{index}",
            claim_binding=binding,
        )
        before = (
            store.get_claim(contract_a.id),
            store.get_claim(contract_b.id),
            len(_fact_types(store)),
        )
        decision = CandidateCommitter(store, store).commit(
            replace(proposal, signature="invalid-test-signature")
        )
        after = (store.get_claim(contract_a.id), store.get_claim(contract_b.id))
        assert decision.accepted is False
        assert after == before[:2]
        assert len(_fact_types(store)) > before[2]
    assert all(
        store.get_claim(contract.id)["status"] == "active"
        for contract in (contract_a, contract_b)
    )
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    assert not store.scan_runtime_records(record_type="claim.submitted")
    assert not store.scan_runtime_records(record_type="attempt.closed")
    store.close()


def test_invalid_signature_cannot_use_expired_or_terminal_claim_binding(tmp_path):
    database = tmp_path / "closed-bindings.sqlite"
    store, root, host, (expired_contract, terminal_contract) = _domain(
        database, ("expired-task", "terminal-task")
    )
    claimed_terminal = _claim(store, host, terminal_contract, "terminal")
    cancelled = root.publish(
        (contract_cancellation_effect(terminal_contract.id, "fixture terminal"),),
        idempotency_key="terminalize-task",
    )
    assert cancelled.accepted
    assert store.get_claim(terminal_contract.id)["status"] == "released"

    expired = _claim(store, host, expired_contract, "expired")
    grant = next(
        record
        for _, record in store.scan_runtime_records(record_type="claim.granted")
        if record.payload["claim_id"] == expired.package.claim_id
    )
    observed = datetime.now(timezone.utc) + timedelta(seconds=1)
    expiration = create_runtime_record(
        "claim.expired",
        {
            "claim_id": expired.package.claim_id,
            "contract_id": expired_contract.id,
            "epoch": expired.package.claim_epoch,
            "lease_until": expired.package.lease_until,
            "package_id": expired.package.package_id,
            "worker_id": host.worker_id,
        },
        causal_parents=(grant.id,),
        recorded_at=observed.isoformat(),
    )
    terminal = create_terminal_record(
        expired_contract.id,
        "failed",
        causal_parents=(expiration.id,),
        recorded_at=observed.isoformat(),
    )
    expired_entry = create_entry(
        expired_contract.id,
        "claim_expired",
        worker_id=host.worker_id,
        authority_result="rejected",
    )
    assert store.commit_claim_expiration(
        trace_entry=expired_entry,
        runtime_records=(expiration, terminal),
        claim_id=expired.package.claim_id,
        claim_epoch=expired.package.claim_epoch,
        expected_lease_until=expired.package.lease_until,
        observed_at=observed.isoformat(),
    )

    for index, claimed in enumerate((expired, claimed_terminal)):
        proposal = _candidate(host, claimed, idempotency=f"bad-closed-binding-{index}")
        before_records = {record.id for _, record in store.scan_runtime_records()}
        before_claim = store.get_claim(claimed.contract.id)
        decision = CandidateCommitter(store, store).commit(
            replace(proposal, signature="invalid-test-signature")
        )
        assert decision.accepted is False
        assert store.get_claim(claimed.contract.id) == before_claim
        new_types = {
            record.record_type
            for _, record in store.scan_runtime_records()
            if record.id not in before_records
        }
        assert new_types <= {"candidate.authentication_rejected", "trace.recorded"}
    assert store.get_claim(expired_contract.id)["status"] == "expired"
    assert not store.scan_runtime_records(record_type="attempt.closed")
    store.close()


def test_bad_signature_reusing_committed_candidate_id_still_gets_audit(tmp_path):
    database = tmp_path / "committed-id-auth-rejection.sqlite"
    store, _root, host, (contract,) = _domain(database)
    claimed = _claim(store, host, contract)
    output = contract.outputs[0]
    raw_output = f'/exec {{"outputs":{{"{output}":"signed result"}}}}'
    envelope = CandidateEnvelope(
        contract.id,
        host.worker_id,
        raw_output,
        parsed_action={"type": "exec", "outputs": {output: "signed result"}},
        package_id=claimed.package.package_id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
        idempotency_key="accepted-then-tampered",
    )
    proposal = _candidate(
        host,
        claimed,
        idempotency="accepted-then-tampered",
        effects=claim_bound_output_effects(envelope),
        raw_output=raw_output,
    )
    committed = CandidateCommitter(store, store).commit(proposal)
    assert committed.accepted
    assert any(
        record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == proposal.id
        for _, record in store.scan_runtime_records()
    )
    before_revision = store.get_runtime_revision()

    rejected = CandidateCommitter(store, store).commit(
        replace(proposal, signature="invalid-test-signature")
    )

    assert rejected.accepted is False
    assert store.get_runtime_revision() > before_revision
    auth_records = [
        record
        for _, record in store.scan_runtime_records(
            record_type="candidate.authentication_rejected"
        )
        if record.payload["claimed_candidate_id"] == proposal.id
    ]
    assert len(auth_records) == 1
    assert store.get_claim(contract.id)["status"] == "submitted"
    assert store.get_assets_by_name(output)
    assert len(store.scan_runtime_records(record_type="attempt.closed")) == 1
    store.close()


def test_signed_projection_rejection_keeps_attempt_and_recovery(tmp_path):
    store, root, host, (contract,) = _domain(tmp_path / "signed-rejection.sqlite")
    claimed = _claim(store, host, contract)
    proposal = _candidate(
        host,
        claimed,
        idempotency="signed-projection-rejection",
        effects=(CandidateEffect("test.unsupported", {}),),
        raw_output="unsupported: retry with a valid action",
    )

    decision = CandidateCommitter(store, store).commit(proposal)

    assert decision.accepted is False
    received = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.received"
    )
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    attempt = next(
        record
        for record in decision.runtime_records
        if record.record_type == "attempt.closed"
    )
    assert rejection.causal_parents == (received.id,)
    assert attempt.causal_parents == (rejection.id,)
    assert any(
        record.record_type == "lifecycle.terminal"
        and record.causal_parents == (rejection.id,)
        for record in decision.runtime_records
    )

    recovery_registry = CandidatePublisherRegistry((("recovery.publish.v1", root),))
    assert process_rejected_submissions(
        store, candidate_publishers=recovery_registry
    ) == [contract.id]
    assert store.scan_runtime_records(
        record_type="candidate_rejection.recovery_scheduled"
    )
    assert any(
        item.origin == "recovery" and item.name == f"{contract.name}.recover"
        for item in store.get_all_contracts()
    )
    store.close()
