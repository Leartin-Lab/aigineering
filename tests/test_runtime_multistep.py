"""End-to-end multi-step planning through the stateless worker protocol."""

from __future__ import annotations

import json

from aigineering.agent.mock import MockWorker
from aigineering.application import default_completion_registry
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration, is_eligible
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    worker_registration_effect,
)
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    process_method_completions,
)


def test_plan_method_and_independent_child_complete_root_from_assets():
    store = SQLiteStore(":memory:")
    plugin_signer = Ed25519Signer()
    plugin_key = ActorKey(
        "plugin:planning.expand.v1",
        "planning-1",
        plugin_signer.kind,
        plugin_signer.signer_id,
        ("contract.publish",),
    )
    genesis = create_genesis_manifest(
        "runtime-multistep", (plugin_key,), "policy:runtime-multistep"
    )
    initialize_genesis(store, genesis)
    plugin_publisher = CandidatePublisher(
        store, store, genesis, plugin_key, plugin_signer
    )
    ingress = RuntimeIngress(store, store)
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="research_report",
            outputs=("final_report",),
            budget=5,
        )
    )
    registry = default_completion_registry()
    assert registry.list_types() == ["fail", "plan", "replan", "tool"]
    worker = MockWorker()

    worker.set_output("research_report", '/plan {"reason":"decompose"}')
    root_claim = claim_next_package(store, worker_id="worker", contract_id=root.id)
    assert root_claim is not None
    scheduled = execute_claimed_package(root_claim, worker, store)
    assert scheduled["status"] == "method_scheduled"

    plan_contract = store.get_contract(scheduled["child_contract_id"])
    assert plan_contract is not None
    plan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft_report",
                    "description": "produce the requested report",
                    "inputs": [],
                    "outputs": ["final_report"],
                    "activation": "",
                    "budget": 2,
                    "tool_scope": [],
                    "labels": [],
                }
            ]
        },
        sort_keys=True,
    )
    worker.set_output(
        plan_contract.name,
        "/exec " + json.dumps({plan_contract.outputs[0]: plan_content}),
    )
    plan_claim = claim_next_package(
        store, worker_id="worker", contract_id=plan_contract.id
    )
    assert plan_claim is not None
    plan_result = execute_claimed_package(plan_claim, worker, store)
    assert plan_result["status"] == "accepted"

    assert process_method_completions(
        store,
        registry,
        candidate_publishers=CandidatePublisherRegistry(
            (("planning.expand.v1", plugin_publisher),)
        ),
    ) == [plan_contract.id]
    planning_receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == plugin_key.actor_id
        and record.payload.get("effect_types") == ("contract.declare",)
    ]
    assert len(planning_receipts) == 1
    planned_children = [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "plan"
    ]
    assert len(planned_children) == 1

    child = planned_children[0]
    worker.set_output(child.name, '/exec {"final_report":"complete report"}')
    child_claim = claim_next_package(store, worker_id="worker", contract_id=child.id)
    assert child_claim is not None
    child_result = execute_claimed_package(child_claim, worker, store)
    assert child_result["status"] == "accepted"

    assert store.has_asset_named("final_report")
    root_terminals = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload["contract_id"] == root.id
    ]
    assert len(root_terminals) == 1
    assert root_terminals[0].payload["terminal"] == "complete"
    store.close()


def test_tool_completion_plugin_publishes_continuation_candidate():
    store = SQLiteStore(":memory:")
    plugin_signer = Ed25519Signer()
    worker_signer = Ed25519Signer()
    worker_key = ActorKey(
        "worker",
        "worker-1",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    plugin_key = ActorKey(
        "plugin:continuation.publish.v1",
        "continuation-1",
        plugin_signer.kind,
        plugin_signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest(
        "runtime-tool", (plugin_key, worker_key), "policy:runtime-tool"
    )
    initialize_genesis(store, genesis)
    publisher = CandidatePublisher(store, store, genesis, plugin_key, plugin_signer)
    registration = publisher.publish(
        (
            worker_registration_effect(
                WorkerRegistration(
                    "worker",
                    capabilities=("tool-execution",),
                    actor_id=worker_key.actor_id,
                    key_id=worker_key.key_id,
                )
            ),
            asset_proposal_effect(
                create_tool_descriptor(
                    "lookup",
                    "Lookup a value.",
                    {"type": "object"},
                    trust_tier="configured",
                )
            ),
        ),
        idempotency_key="register-tool-worker",
    )
    assert registration.accepted is True
    publishers = CandidatePublisherRegistry((("continuation.publish.v1", publisher),))
    ingress = RuntimeIngress(store, store)
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="tool_report",
            outputs=("final_report",),
            tool_scope=("lookup",),
            budget=4,
        )
    )
    worker = MockWorker()
    worker.set_output(
        root.name,
        '/tool {"name":"lookup","args":{"key":"x"}}',
    )
    root_claim = claim_next_package(store, worker_id="worker", contract_id=root.id)
    assert root_claim is not None
    scheduled = execute_claimed_package(root_claim, worker, store)
    tool_contract = store.get_contract(scheduled["child_contract_id"])
    assert tool_contract is not None
    observation = json.dumps(
        {"ok": True, "tool": "lookup", "result": "value:x"},
        sort_keys=True,
    )
    worker.set_output(
        tool_contract.name,
        "/exec " + json.dumps({tool_contract.outputs[0]: observation}),
    )
    assert store.has_asset_named(tool_contract.activation)
    tool_view = RuntimeProjection(store, store).contract_view(tool_contract)
    assert tool_view.enabled, tool_view.blockers
    registration_view = store.get_worker_registration("worker")
    assert registration_view is not None
    assert is_eligible(tool_contract, registration_view)
    tool_claim = claim_next_package(
        store, worker_id="worker", contract_id=tool_contract.id
    )
    assert tool_claim is not None
    assert execute_claimed_package(tool_claim, worker, store)["status"] == "accepted"

    processed = process_method_completions(
        store,
        default_completion_registry(),
        candidate_publishers=publishers,
    )

    assert processed == [tool_contract.id]
    continuations = [
        contract
        for contract in store.get_all_contracts()
        if contract.origin == "continuation"
    ]
    assert len(continuations) == 1
    receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == plugin_key.actor_id
        and record.payload.get("effect_types") == ("contract.declare",)
    ]
    assert len(receipts) == 1
