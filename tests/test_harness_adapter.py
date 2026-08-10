"""Existing agent harnesses can publish through the authenticated Worker boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aigineering.agent.harness import (
    HarnessCandidateAdapter,
    candidate_dict,
    candidate_json,
)
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_coordination import authenticate_worker_command
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package, submit_worker_proposal


def _runtime(
    db_path: str = ":memory:",
) -> tuple[SQLiteStore, Contract, HarnessCandidateAdapter]:
    store = SQLiteStore(db_path)
    signer = Ed25519Signer()
    actor = ActorKey(
        "harness:codex",
        "codex-1",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "worker.register", "worker.submit"),
    )
    genesis = create_genesis_manifest(
        "harness-adapter-test", (actor,), "policy:harness-adapter-test"
    )
    initialize_genesis(store, genesis)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        worker_registration_effect(
            WorkerRegistration(
                actor.actor_id,
                profile_id="codex-harness-v1",
                actor_id=actor.actor_id,
                key_id=actor.key_id,
            )
        ),
        idempotency_key="register-codex-harness",
    )
    fields = {
        "name": "harness_task",
        "description": "Return a trustworthy harness result.",
        "inputs": (),
        "outputs": ("trusted_result",),
        "activation": "",
        "budget": 5,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="publish-harness-task",
    )
    return store, contract, HarnessCandidateAdapter(genesis.id, actor, signer)


def _claim(store, contract, adapter):
    request = adapter.claim_candidate(
        request_id="claim-1",
        contract_id=contract.id,
    )
    command = authenticate_worker_command(request, "worker.claim", store)
    claimed = claim_next_package(
        store,
        worker_id=adapter.worker_id,
        contract_id=contract.id,
        claim_runtime_records=command.runtime_records,
    )
    assert claimed is not None
    return claimed


def test_harness_claim_and_result_use_one_signed_candidate_path():
    store, contract, adapter = _runtime()
    claimed = _claim(store, contract, adapter)

    proposal = adapter.result_candidate(
        json.loads(claimed.package.to_json()),
        '/exec {"outputs":{"trusted_result":"verified by harness"}}',
        usage_metadata={"model": "harness-model", "total_tokens": 17},
    )
    result = submit_worker_proposal(proposal, store)

    assert result["status"] == "accepted"
    assert {effect.effect_type for effect in proposal.effects} == {
        "asset.content.publish",
        "asset.definition.publish",
        "asset.assert",
    }
    assert store.get_assets_by_name("trusted_result")[-1].content == (
        "verified by harness"
    )
    assert store.get_claim(contract.id)["status"] == "submitted"
    record_types = {record.record_type for _, record in store.scan_runtime_records()}
    assert "worker.claim.requested" in record_types


def test_harness_result_reconstructs_after_store_reopen(tmp_path: Path):
    db_path = str(tmp_path / "harness.db")
    store, contract, adapter = _runtime(db_path)
    claimed = _claim(store, contract, adapter)
    proposal = adapter.result_candidate(
        claimed.package,
        '/exec {"trusted_result":"durable"}',
    )
    assert submit_worker_proposal(proposal, store)["status"] == "accepted"
    store.close()

    reopened = SQLiteStore(db_path)
    restored_contract = reopened.get_contract(contract.id)
    assert restored_contract is not None
    view = RuntimeProjection(reopened, reopened).contract_view(restored_contract)

    assert view.outputs_satisfied is True
    assert view.terminal == "complete"
    assert reopened.get_assets_by_name("trusted_result")[-1].content == "durable"


def test_harness_plan_uses_the_same_recursive_task_compiler():
    store, contract, adapter = _runtime()
    claimed = _claim(store, contract, adapter)

    proposal = adapter.result_candidate(
        claimed.package,
        '/plan {"reason":"split trustworthy work"}',
    )

    assert len(proposal.effects) == 3
    assert {effect.effect_type for effect in proposal.effects} == {"contract.declare"}


def test_harness_renewal_binds_claim_epoch_and_worker_identity():
    store, contract, adapter = _runtime()
    claimed = _claim(store, contract, adapter)

    renewal = adapter.renewal_candidate(
        claimed.package,
        request_id="renew-1",
        lease_seconds=90,
    )
    command = authenticate_worker_command(renewal, "worker.claim.renew", store)

    assert command.payload["worker_id"] == adapter.worker_id
    assert command.payload["claim_id"] == claimed.package.claim_id
    assert command.payload["claim_epoch"] == claimed.package.claim_epoch


def test_harness_adapter_rejects_wrong_signer_and_unclaimed_result():
    store, contract, adapter = _runtime()
    del store, contract
    with pytest.raises(ValueError, match="does not match"):
        HarnessCandidateAdapter(
            adapter.domain_id,
            adapter.actor_key,
            Ed25519Signer(),
        )

    package = {
        "protocol_version": 3,
        "contract_id": "missing-claim",
        "contract": {"id": "missing-claim"},
        "disclosed_assets": [],
        "method_context_assets": [],
        "tool_scope": [],
        "budget_remaining": 1,
    }
    with pytest.raises(ValueError, match="claimed WorkerPackage"):
        adapter.result_candidate(package, '/exec {"result":"no"}')


def test_harness_candidate_json_contains_public_key_signature_not_private_key():
    store, contract, adapter = _runtime()
    claimed = _claim(store, contract, adapter)
    proposal = adapter.result_candidate(
        claimed.package,
        '/exec {"trusted_result":"done"}',
    )

    payload = candidate_json(proposal)

    assert candidate_dict(proposal)["id"] == proposal.id
    assert proposal.signature in payload
    assert adapter.signer.signer_id not in payload
    assert "private" not in payload.lower()
