"""Humans participate as keyed Workers, never as a privileged write path."""

from __future__ import annotations

from conftest import candidate_runtime

from aigineering.agent.worker import WorkerHost
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
)
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.types import Asset, Candidate, Contract
from aigineering.runtime import claim_next_package, execute_claimed_package


class _HumanReviewWorker:
    worker_id = "human:reviewer"

    def invoke(self, contract, disclosed_assets):
        assert contract.name == "human_release_review"
        assert {asset.name for asset in disclosed_assets} == {"release_evidence"}
        return Candidate(
            worker_id=self.worker_id,
            raw_output='/exec {"outputs":{"human_decision":"approved with evidence"}}',
        )


def test_human_worker_claims_and_completes_through_signed_candidate() -> None:
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    runtime.accept_asset(
        Asset(
            id="human:evidence",
            name="release_evidence",
            content="deterministic gates passed",
        )
    )
    contract = runtime.accept_contract(
        Contract(
            id="human:review",
            name="human_release_review",
            inputs=("release_evidence",),
            outputs=("human_decision",),
            activation="release_evidence",
            budget=1,
            worker_capabilities=("review.human",),
        )
    )
    signer = Ed25519Signer()
    key = runtime.authorize_actor(
        ActorKey(
            _HumanReviewWorker.worker_id,
            "human-review-key",
            signer.kind,
            signer.signer_id,
            ("worker.submit",),
        )
    )
    runtime.register_worker(
        WorkerRegistration(
            _HumanReviewWorker.worker_id,
            capabilities=("review.human",),
            actor_id=key.actor_id,
            key_id=key.key_id,
        )
    )
    host = WorkerHost(_HumanReviewWorker(), runtime.genesis, key, signer)
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "accepted"
    output = store.get_assets_by_name("human_decision")
    assert len(output) == 1
    assert output[0].created_by == contract.id


def test_human_identity_cannot_be_impersonated_by_another_private_key() -> None:
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    human_signer = Ed25519Signer()
    human_key = runtime.authorize_actor(
        ActorKey(
            "human:reviewer",
            "human-review-key",
            human_signer.kind,
            human_signer.signer_id,
            ("asset.publish",),
        )
    )
    impostor = Ed25519Signer()
    proposal = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=human_key.actor_id,
        key_id=human_key.key_id,
        effects=(
            asset_proposal_effect(
                Asset(id="forged", name="human_assertion", content="approved")
            ),
        ),
        signer=impostor,
        idempotency_key="forged-human-assertion",
    )

    decision = CandidateCommitter(store, store).commit(proposal, runtime.genesis)

    assert not decision.accepted
    assert any(
        record.record_type == "candidate.authentication_rejected"
        for record in decision.runtime_records
    )
    assert store.get_assets_by_name("human_assertion") == []
