"""Authenticated WorkerHost conformance for ordinary and transitional actions."""

from __future__ import annotations

import sqlite3

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
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Contract
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.runtime import (
    WorkerInvocationError,
    WorkerSubmissionCommitError,
    claim_next_package,
    execute_claimed_package,
    submit_worker_proposal,
)


def _runtime(output: str, *, budget: int = 3):
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
        "budget": budget,
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
        and set(record.payload.get("effect_types", ()))
        == {
            "asset.assert",
            "asset.content.publish",
            "asset.definition.publish",
        }
    )
    output = next(
        record
        for _, record in store.scan_runtime_records(record_type="asset.committed")
        if record.payload["asset"]["name"] == "result"
    )
    assertion = store.scan_runtime_records(
        record_type="asset.definition-content.asserted"
    )[-1][1]
    assert assertion.causal_parents == (receipt.id,)
    assert output.causal_parents == (assertion.id,)
    asset = store.get_assets_by_name("result")[-1]
    assert asset.id.startswith("asset:v1:")
    assert asset.definition_hash.startswith("definition:v1:")
    assert asset.content_hash.startswith("content:v1:")
    assert store.get_claim(contract.id)["status"] == "submitted"


def test_worker_outputs_share_content_without_collapsing_assertion_identity():
    store, first, original_host = _runtime('/exec {"result":"same"}')
    fields = {
        "name": "hosted_task_two",
        "description": "Second provenance for equal content",
        "inputs": (),
        "outputs": ("result_two",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    second = Contract(id=hash_contract_v3(**fields), **fields)
    store.add_contract(second)
    host = WorkerHost(
        MockWorker(
            {
                first.name: '/exec {"result":"same"}',
                second.name: '/exec {"result_two":"same"}',
            },
            worker_id=original_host.worker_id,
        ),
        original_host.genesis,
        original_host.actor_key,
        original_host.signer,
    )

    for _ in range(2):
        claimed = claim_next_package(store, worker_id=host.worker_id)
        assert claimed is not None
        assert execute_claimed_package(claimed, host, store)["status"] == "accepted"

    assert len(store.get_content_objects()) == 1
    assert len(store.get_asset_definitions()) == 2
    assert len(store.get_definition_content_assertions()) == 2
    assets = store.get_all_assets()
    assert len(assets) == 2
    assert len({asset.id for asset in assets}) == 2
    assert len({asset.content_hash for asset in assets}) == 1
    assert len({asset.definition_hash for asset in assets}) == 2


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


def test_worker_host_classifies_plan_request_without_enough_allowance():
    store, contract, host = _runtime('/plan {"reason":"split"}', budget=2)
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(claimed, host, store)

    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[-1][1]
    assert failure.payload["category"] == "worker_error:planning_request_rejected"
    assert store.get_claim(contract.id)["status"] == "released"


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


def test_claim_bound_graph_output_rejects_partial_endpoint_batch():
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
        idempotency_key=f"partial-{claimed.package.package_id}",
    )
    complete = host.sign_envelope(envelope, contract=contract)
    partial = create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=complete.effects[:-1],
        signer=host.signer,
        idempotency_key=envelope.idempotency_key,
        claim_binding=complete.claim_binding,
    )

    result = submit_worker_proposal(partial, store)

    assert result["status"] == "rejected"
    assert "unsupported effect" in result["rejected"][0]
    assert store.get_content_objects() == []
    assert store.get_asset_definitions() == []
    assert store.get_definition_content_assertions() == []
    assert store.get_assets_by_name("result") == []


def test_claim_bound_candidate_rejects_stale_epoch_before_projection():
    store, contract, host = _runtime('/exec {"result":"done"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id=host.worker_id,
        raw_output='/exec {"result":"done"}',
        package_id=claimed.package.package_id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch + 1,
        idempotency_key=f"stale-{claimed.package.package_id}",
    )

    with pytest.raises(ValueError, match="active worker claim predicate failed"):
        submit_worker_proposal(host.sign_envelope(envelope, contract=contract), store)

    assert store.get_assets_by_name("result") == []
    assert store.get_claim(contract.id)["status"] == "active"


def test_claim_bound_candidate_rechecks_claim_state_inside_commit():
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
        idempotency_key=f"released-{claimed.package.package_id}",
    )
    proposal = host.sign_envelope(envelope, contract=contract)
    store.mark_claim_released(claimed.package.claim_id)

    with pytest.raises(ValueError, match="active worker claim predicate failed"):
        submit_worker_proposal(proposal, store)

    assert store.get_assets_by_name("result") == []
    assert store.get_claim(contract.id)["status"] == "released"


def test_terminal_contract_rejects_candidate_signed_under_older_claim():
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
        idempotency_key=f"terminal-{claimed.package.package_id}",
    )
    proposal = host.sign_envelope(envelope, contract=contract)
    store.append_runtime_record(
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": contract.id, "terminal": "cancelled"},
        )
    )

    with pytest.raises(ValueError, match="terminal Contract"):
        submit_worker_proposal(proposal, store)

    assert store.get_assets_by_name("result") == []
    assert store.get_claim(contract.id)["status"] == "released"
    rejection = store.scan_runtime_records(record_type="candidate.rejected")[-1][1]
    assert rejection.payload["candidate_id"] == proposal.id


def test_submission_infrastructure_failure_is_not_candidate_rejection(monkeypatch):
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
        idempotency_key=f"infra-{claimed.package.package_id}",
    )
    proposal = host.sign_envelope(envelope, contract=contract)

    def fail_commit(*_args, **_kwargs):
        raise sqlite3.OperationalError("storage unavailable")

    monkeypatch.setattr("aigineering.runtime.CandidateCommitter.commit", fail_commit)

    with pytest.raises(WorkerSubmissionCommitError, match="storage unavailable"):
        submit_worker_proposal(proposal, store)

    assert store.scan_runtime_records(record_type="candidate.rejected") == []
    assert store.get_claim(contract.id)["status"] == "active"


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


@pytest.mark.parametrize("effect_type", ["worker.output", "task.delegate"])
def test_claim_bound_legacy_wrapper_is_rejected_and_closes_attempt(effect_type):
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
    effect = CandidateEffect(effect_type, {"envelope": envelope.to_dict()})
    proposal = create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=(effect,),
        signer=host.signer,
        idempotency_key=envelope.idempotency_key,
        claim_binding=CandidateClaimBinding(
            claimed.contract.id,
            claimed.package.claim_id,
            claimed.package.claim_epoch,
            claimed.package.package_id,
        ),
    )

    result = submit_worker_proposal(proposal, store)

    assert result["status"] == "rejected"
    assert any("unsupported effect" in reason for reason in result["rejected"])
    assert store.get_claim(claimed.contract.id)["status"] == "submitted"
    assert store.get_contract(claimed.contract.id) is not None
    assert len(store.get_all_contracts()) == 1
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")[-1][1]
    assert terminal.payload["terminal"] == "failed"
