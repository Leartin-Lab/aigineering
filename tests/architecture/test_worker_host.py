"""Authenticated WorkerHost conformance for ordinary and transitional actions."""

from __future__ import annotations

import pytest

from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerHost
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    worker_output_effect,
    worker_registration_effect,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Contract
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    submit_worker_proposal,
)


def _runtime(output: str):
    store = SQLiteStore(":memory:")
    signer = Ed25519Signer()
    actor = ActorKey(
        "worker:hosted",
        "hosted-1",
        signer.kind,
        signer.signer_id,
        ("worker.register", "worker.submit"),
    )
    genesis = create_genesis_manifest(
        "worker-host-test", (actor,), "policy:worker-host-test"
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
                actor_id=actor.actor_id,
                key_id=actor.key_id,
            )
        ),
        idempotency_key="register-hosted-worker",
    )
    fields = {
        "name": "hosted_task",
        "description": "Exercise a hosted worker",
        "inputs": (),
        "outputs": ("result",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    store.add_contract(contract)
    worker = MockWorker({contract.name: output}, worker_id=actor.actor_id)
    return store, contract, WorkerHost(worker, genesis, actor, signer)


def test_worker_host_signs_ordinary_claim_submission():
    store, contract, host = _runtime('/exec {"result":"done"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "accepted"
    receipt = next(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == host.worker_id
        and record.payload.get("effect_types") == ("asset.propose",)
    )
    output = next(
        record
        for _, record in store.scan_runtime_records(record_type="asset.committed")
        if record.payload["asset"]["name"] == "result"
    )
    assert output.causal_parents == (receipt.id,)
    assert store.get_claim(contract.id)["status"] == "submitted"


def test_invalid_host_action_closes_claim_instead_of_ending_silently():
    store, contract, host = _runtime("/unsupported {}")
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    with pytest.raises(WorkerInvocationError, match="claim was released"):
        execute_claimed_package(claimed, host, store)

    assert store.get_claim(contract.id)["status"] == "released"
    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[-1][1]
    assert failure.payload["category"] == "worker_error:invalid_action"
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")[-1][1]
    assert terminal.payload["terminal"] == "failed"


def test_worker_host_plan_publishes_three_claim_bound_stage_contracts():
    store, contract, host = _runtime('/plan {"reason":"split review"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "task_delegated"
    assert len(result["child_contract_ids"]) == 3
    children = [
        item for item in store.get_all_contracts() if item.parent_id == contract.id
    ]
    assert {
        label
        for item in children
        for label in item.labels
        if label.startswith("plugin:")
    } == {
        "plugin:plan.draft",
        "plugin:plan.dependencies",
        "plugin:plan.compile",
    }
    assert store.scan_runtime_records(record_type="task.delegated") == []
    assert store.get_claim(contract.id)["status"] == "submitted"
    assert not any(
        record.payload.get("contract_id") == contract.id
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    )
    attempt = store.scan_runtime_records(record_type="attempt.closed")[-1][1]
    assert attempt.payload["outcome"] == "expanded"


def test_claim_bound_candidate_replay_is_idempotent_after_claim_closes():
    store, contract, host = _runtime('/exec {"result":"done"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id=host.worker_id,
        raw_output='/exec {"result":"done"}',
        package_id=claimed.package.package_id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
        idempotency_key=f"run-{claimed.package.package_id}",
    )
    proposal = host.sign_envelope(envelope, contract=contract)

    first = submit_worker_proposal(proposal, store)
    second = submit_worker_proposal(proposal, store)

    assert first["status"] == second["status"] == "accepted"
    committed = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.committed")
        if record.payload.get("candidate_id") == proposal.id
    ]
    assert len(committed) == 1


def test_claim_bound_expansion_cannot_widen_parent_tool_scope():
    store, contract, host = _runtime('/plan {"reason":"split"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
    fields = {
        "name": "malicious_child",
        "description": "widen tool scope",
        "inputs": (),
        "outputs": ("child_result",),
        "activation": "",
        "budget": 1,
        "tool_scope": ("undelegated_tool",),
        "labels": (),
        "origin": "plugin",
        "parent_id": contract.id,
    }
    child = Contract(id=hash_contract_v3(**fields), **fields)
    binding = CandidateClaimBinding(
        contract.id,
        claimed.package.claim_id,
        claimed.package.claim_epoch,
        claimed.package.package_id,
    )
    proposal = create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=(contract_declaration_effect(child),),
        signer=host.signer,
        idempotency_key=f"run-{claimed.package.package_id}",
        claim_binding=binding,
    )

    result = submit_worker_proposal(proposal, store)

    assert result["status"] == "rejected"
    assert store.get_contract(child.id) is None
    assert store.get_claim(contract.id)["status"] == "submitted"
    attempt = store.scan_runtime_records(record_type="attempt.closed")[-1][1]
    assert attempt.payload["outcome"] == "failed"
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")[-1][1]
    assert terminal.payload == {
        "contract_id": contract.id,
        "reason": "claim-bound Candidate was rejected",
        "terminal": "failed",
    }


def test_claim_bound_child_cannot_self_grant_protected_authority():
    store, contract, host = _runtime('/plan {"reason":"split"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
    fields = {
        "name": "forged_system_child",
        "description": "attempt protected self-grant",
        "inputs": (),
        "outputs": ("_sys_forged",),
        "activation": "",
        "budget": 1,
        "tool_scope": (),
        "labels": ("plugin:forged",),
        "origin": "system",
        "parent_id": contract.id,
        "minting_authority": ("_sys_forged",),
    }
    child = Contract(id=hash_contract_v3(**fields), **fields)
    binding = CandidateClaimBinding(
        contract.id,
        claimed.package.claim_id,
        claimed.package.claim_epoch,
        claimed.package.package_id,
    )
    proposal = create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=(contract_declaration_effect(child),),
        signer=host.signer,
        idempotency_key=f"run-{claimed.package.package_id}",
        claim_binding=binding,
    )

    result = submit_worker_proposal(proposal, store)

    assert result["status"] == "rejected"
    assert store.get_contract(child.id) is None
    rejection = store.scan_runtime_records(record_type="candidate.rejected")[-1][1]
    assert "contract.publish.protected" in rejection.payload["reason"]


def test_worker_host_delegates_without_method_handler_authorization():
    store, contract, host = _runtime('/retry {"reason":"try again"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "task_delegated"
    receipt = next(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("effect_types") == ("contract.declare",)
    )
    declared = store.scan_runtime_records(record_type="contract.declared")[-1][1]
    assert declared.causal_parents == (receipt.id,)
    child = store.get_contract(result["child_contract_id"])
    assert child is not None
    assert child.parent_id == contract.id
    assert "plugin:retry" in child.labels
    assert not store.scan_runtime_records(record_type="task.delegated")
    assert store.get_claim(contract.id)["status"] == "submitted"


def test_signed_output_effect_cannot_be_reinterpreted_as_task_delegation():
    store, _contract, host = _runtime('/retry {"reason":"try again"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
    envelope = CandidateEnvelope(
        contract_id=claimed.contract.id,
        worker_id=host.worker_id,
        raw_output='/retry {"reason":"try again"}',
        package_id=claimed.package.package_id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
        idempotency_key=f"run-{claimed.package.package_id}",
    )
    proposal = create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=(worker_output_effect(envelope),),
        signer=host.signer,
        idempotency_key=envelope.idempotency_key,
    )

    try:
        submit_worker_proposal(
            proposal,
            store,
        )
    except ValueError as exc:
        assert "requires a 'task.delegate' effect" in str(exc)
    else:
        raise AssertionError("ordinary output effect was accepted as delegation")

    assert store.get_claim(claimed.contract.id)["status"] == "active"
    assert store.get_contract(claimed.contract.id) is not None
    assert len(store.get_all_contracts()) == 1
