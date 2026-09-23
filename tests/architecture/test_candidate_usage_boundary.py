"""Authenticated admission and recovery invariants for usage metadata."""

import json
from dataclasses import replace

import pytest
from conftest import hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import contract_identity_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
    WORKER_RAW_OUTPUT_METADATA_KEY,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package, submit_worker_proposal


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "secret-marker"},
        {"total_tokens": True},
        {"secret-field-name": "secret-marker"},
        {"purpose": {"private": "secret-marker"}},
        {"purpose": None},
    ],
)
def test_claim_bound_signed_invalid_usage_is_rejected_with_safe_receipt(metadata):
    store = SQLiteStore(":memory:")
    try:
        authority_signer = Ed25519Signer()
        authority_key = ActorKey(
            "test:usage-authority",
            "usage-authority-key",
            authority_signer.kind,
            authority_signer.signer_id,
            (
                "actor.authorize",
                "asset.publish",
                "asset.publish.protected",
                "contract.publish",
                "contract.publish.protected",
                "worker.register",
            ),
        )
        recovery_signer = Ed25519Signer()
        recovery_actor = ActorKey(
            "plugin:recovery.publish.usage-test",
            "recovery-usage-test",
            recovery_signer.kind,
            recovery_signer.signer_id,
            (
                "asset.publish",
                "asset.publish.protected",
                "contract.publish",
                "contract.publish.protected",
            ),
        )
        genesis = create_genesis_manifest(
            "usage-test-domain",
            (authority_key, recovery_actor),
            "policy:usage-test",
        )
        initialize_genesis(store, genesis)
        authority = CandidatePublisher(
            store, store, genesis, authority_key, authority_signer
        )
        contract = Contract(id="task:bad-usage", outputs=("report",), budget=1)
        contract = replace(contract, id=contract_identity_v3(contract))
        declaration = authority.publish(
            (contract_declaration_effect(contract),),
            idempotency_key="usage-test-contract",
        )
        assert declaration.accepted
        contract = declaration.contract
        host = hosted_worker(
            store,
            MockWorker(worker_id="worker:bad-usage"),
            genesis=genesis,
            authority_key=authority_key,
            authority_signer=authority_signer,
        )
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=contract.id,
        )
        assert claimed is not None
        recovery_publishers = CandidatePublisherRegistry(
            (
                (
                    "recovery.publish.v1",
                    CandidatePublisher(
                        store, store, genesis, recovery_actor, recovery_signer
                    ),
                ),
            )
        )
        signed = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=host.actor_key.actor_id,
            key_id=host.actor_key.key_id,
            effects=(CandidateEffect("test.usage_invalid", {"ignored": True}),),
            signer=host.signer,
            claim_binding=CandidateClaimBinding(
                contract_id=contract.id,
                claim_id=claimed.package.claim_id,
                claim_epoch=claimed.package.claim_epoch,
                package_id=claimed.package.package_id,
            ),
            idempotency_key="usage-invalid-candidate",
            metadata={
                **metadata,
                WORKER_RAW_OUTPUT_METADATA_KEY: '/exec {"outputs":{"report":"x"}}',
            },
        )
        result = submit_worker_proposal(
            signed, store, candidate_publishers=recovery_publishers
        )

        assert result["status"] == "rejected"
        records = [record for _, record in store.scan_runtime_records()]
        assert any(record.record_type == "candidate.received" for record in records)
        assert any(record.record_type == "candidate.rejected" for record in records)
        assert any(
            record.record_type == "candidate_rejection.recovery_scheduled"
            for record in records
        )
        assert any(record.record_type == "attempt.closed" for record in records)
        receipt = next(
            record
            for record in records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == signed.id
        )
        assert receipt.payload["metadata"] == {}
        assert receipt.payload["metadata_validation_failed"] is True
        assert receipt.payload["raw_output"] == '/exec {"outputs":{"report":"x"}}'
        assert store.get_claim(contract.id)["status"] == "submitted"
        assert all(trace.usage_metadata is None for trace in store.get_trace_events())
        serialized = json.dumps(
            [deep_thaw(record.payload) for record in records], sort_keys=True
        )
        assert "secret-marker" not in serialized
        assert "api_key" not in serialized
        assert "secret-field-name" not in serialized
        assert '"private"' not in serialized
        assert store.get_contract(contract.id) is not None
    finally:
        store.close()
