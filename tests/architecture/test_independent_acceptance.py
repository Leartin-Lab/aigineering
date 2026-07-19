"""Independent acceptance must bind a different verifier to one exact Asset."""

from __future__ import annotations

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from aigineering.core.commitment import CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.agent.worker import WorkerHost
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.protocol.types import Candidate, Contract


@pytest.fixture
def acceptance_domain(tmp_path):
    db_path = str(tmp_path / "acceptance.db")
    store = SQLiteStore(db_path)
    owner = Ed25519Signer()
    producer = Ed25519Signer()
    verifier = Ed25519Signer()
    weak_verifier = Ed25519Signer()
    verifier_two = Ed25519Signer()
    genesis = create_genesis_manifest(
        "independent-sqlite",
        (
            ActorKey(
                "human:owner",
                "owner-key",
                owner.kind,
                owner.signer_id,
                ("contract.publish", "worker.register"),
            ),
            ActorKey(
                "worker:producer",
                "producer-key",
                producer.kind,
                producer.signer_id,
                ("worker.submit", "asset.attest", "verify.compliance"),
            ),
            ActorKey(
                "worker:verifier",
                "verifier-key",
                verifier.kind,
                verifier.signer_id,
                ("asset.attest", "verify.compliance"),
            ),
            ActorKey(
                "worker:weak-verifier",
                "weak-key",
                weak_verifier.kind,
                weak_verifier.signer_id,
                ("asset.attest",),
            ),
            ActorKey(
                "worker:verifier-two",
                "verifier-two-key",
                verifier_two.kind,
                verifier_two.signer_id,
                ("asset.attest", "verify.compliance"),
            ),
        ),
        "policy:independent-acceptance",
    )
    initialize_genesis(store, genesis)
    committer = CandidateCommitter(store, store)
    values = (
        store,
        genesis,
        committer,
        owner,
        producer,
        verifier,
        weak_verifier,
        verifier_two,
        db_path,
    )
    try:
        yield values
    finally:
        store.close()


def _candidate(genesis, actor_id, key_id, signer, effect, nonce):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_id,
        key_id=key_id,
        effects=(effect,),
        signer=signer,
        idempotency_key=nonce,
    )


def _publish_contract_and_output(domain):
    store, genesis, committer, owner, producer, _, _, _, _ = domain
    fields = {
        "name": "independently_reviewed_report",
        "description": "Publish a report that another actor must verify.",
        "inputs": (),
        "outputs": ("report",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
        "acceptance_policy": {
            "mode": "independent",
            "required_attestations": 1,
            "verifier_capabilities": ["verify.compliance"],
        },
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    declared = _candidate(
        genesis,
        "human:owner",
        "owner-key",
        owner,
        contract_declaration_effect(contract),
        "declare",
    )
    assert committer.commit(declared, genesis).accepted
    registration = WorkerRegistration(
        "worker:producer",
        actor_id="worker:producer",
        key_id="producer-key",
    )
    registered = _candidate(
        genesis,
        "human:owner",
        "owner-key",
        owner,
        worker_registration_effect(registration),
        "register-producer",
    )
    assert committer.commit(registered, genesis).accepted
    claimed = claim_next_package(store, worker_id="worker:producer")
    assert claimed is not None

    class _Producer:
        worker_id = "worker:producer"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/exec {"outputs":{"report":"candidate report"}}',
            )

    host = WorkerHost(
        _Producer(),
        genesis,
        next(key for key in genesis.root_keys if key.actor_id == "worker:producer"),
        producer,
    )
    result = execute_claimed_package(claimed, host, store)
    assert result["status"] == "accepted"
    asset = store.get_assets_by_name("report")[0]
    assert asset.created_by == contract.id
    assert not all_outputs_satisfied(contract, store)
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    return contract, asset


def test_producer_cannot_attest_its_own_output(acceptance_domain):
    store, genesis, committer, _, producer, _, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    self_attestation = _candidate(
        genesis,
        "worker:producer",
        "producer-key",
        producer,
        asset_attestation_effect(contract.id, "report", asset.id),
        "self-attest",
    )
    decision = committer.commit(self_attestation, genesis)
    assert not decision.accepted
    assert "cannot attest its own output" in str(decision.runtime_records[1].payload)
    assert not all_outputs_satisfied(contract, store)


def test_verifier_capability_and_exact_asset_qualification(acceptance_domain):
    store, genesis, committer, _, _, verifier, weak_verifier, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    weak = _candidate(
        genesis,
        "worker:weak-verifier",
        "weak-key",
        weak_verifier,
        asset_attestation_effect(contract.id, "report", asset.id),
        "weak-attest",
    )
    assert not committer.commit(weak, genesis).accepted
    exact = _candidate(
        genesis,
        "worker:verifier",
        "verifier-key",
        verifier,
        asset_attestation_effect(contract.id, "report", asset.id),
        "verified-attest",
    )
    decision = committer.commit(exact, genesis)
    assert decision.accepted
    assert any(r.record_type == "asset.attested" for r in decision.runtime_records)
    qualification = next(
        r for r in decision.runtime_records if r.record_type == "output.qualified"
    )
    assert qualification.payload["asset_id"] == asset.id
    assert all_outputs_satisfied(contract, store)
    terminals = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert len(terminals) == 1
    assert terminals[0][1].payload == {
        "contract_id": contract.id,
        "terminal": "complete",
    }


def test_rejected_attestation_is_evidence_not_qualification(acceptance_domain):
    store, genesis, committer, _, _, verifier, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    rejected = _candidate(
        genesis,
        "worker:verifier",
        "verifier-key",
        verifier,
        asset_attestation_effect(contract.id, "report", asset.id, verdict="rejected"),
        "reject-attest",
    )
    decision = committer.commit(rejected, genesis)
    assert decision.accepted
    assert any(r.record_type == "asset.attested" for r in decision.runtime_records)
    assert not any(
        r.record_type == "output.qualified" for r in decision.runtime_records
    )
    assert not all_outputs_satisfied(contract, store)


def test_concurrent_independent_attestations_converge_without_terminal_conflict(
    acceptance_domain,
):
    (
        store,
        genesis,
        _,
        _,
        _,
        verifier,
        _,
        verifier_two,
        db_path,
    ) = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    candidates = (
        _candidate(
            genesis,
            "worker:verifier",
            "verifier-key",
            verifier,
            asset_attestation_effect(contract.id, "report", asset.id),
            "concurrent-one",
        ),
        _candidate(
            genesis,
            "worker:verifier-two",
            "verifier-two-key",
            verifier_two,
            asset_attestation_effect(contract.id, "report", asset.id),
            "concurrent-two",
        ),
    )
    stores = (SQLiteStore(db_path), SQLiteStore(db_path))
    barrier = Barrier(2)
    for current in stores:
        original = current.commit_ingress_batch

        def synchronized_commit(*args, _original=original, **kwargs):
            barrier.wait(timeout=5)
            return _original(*args, **kwargs)

        current.commit_ingress_batch = synchronized_commit
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = tuple(
                pool.map(
                    lambda pair: CandidateCommitter(pair[0], pair[0]).commit(
                        pair[1], genesis
                    ),
                    zip(stores, candidates, strict=True),
                )
            )
        assert all(decision.accepted for decision in decisions)
        assert len(store.scan_runtime_records(record_type="output.qualified")) == 2
        assert len(store.scan_runtime_records(record_type="lifecycle.terminal")) == 1
        assert all_outputs_satisfied(contract, store)
    finally:
        for current in stores:
            current.close()
