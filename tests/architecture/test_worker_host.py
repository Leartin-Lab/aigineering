"""Authenticated WorkerHost conformance for ordinary and transitional actions."""

from __future__ import annotations

from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerHost
from aigineering.application import default_method_registry
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    worker_output_effect,
    worker_registration_effect,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Contract
from aigineering.runtime import (
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
        "budget": 2,
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
        and record.payload.get("effect_types") == ("worker.output",)
    )
    output = store.scan_runtime_records(record_type="worker.output.received")[-1][1]
    assert output.causal_parents == (receipt.id,)
    assert store.get_claim(contract.id)["status"] == "submitted"


def test_worker_host_authenticates_transitional_method_submission():
    store, contract, host = _runtime('/retry {"reason":"try again"}')
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    result = execute_claimed_package(
        claimed,
        host,
        store,
        method_registry=default_method_registry(),
    )

    assert result["status"] == "method_scheduled"
    receipt = next(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("effect_types") == ("task.delegate",)
    )
    method = store.scan_runtime_records(record_type="method.scheduled")[-1][1]
    delegation = store.scan_runtime_records(record_type="worker.delegation.received")[
        -1
    ][1]
    assert delegation.causal_parents == (receipt.id,)
    assert method.causal_parents == (delegation.id,)
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
            method_registry=default_method_registry(),
        )
    except ValueError as exc:
        assert "requires a 'task.delegate' effect" in str(exc)
    else:
        raise AssertionError("ordinary output effect was accepted as delegation")

    assert store.get_claim(claimed.contract.id)["status"] == "active"
    assert store.get_contract(claimed.contract.id) is not None
    assert len(store.get_all_contracts()) == 1
