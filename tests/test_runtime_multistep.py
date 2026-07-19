"""End-to-end multi-step planning through the stateless worker protocol."""

from __future__ import annotations

import json
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerHost
from aigineering.application import default_completion_registry
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
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


def test_plan_action_expands_to_three_independent_stage_tasks():
    store = SQLiteStore(":memory:")
    plugin_signer = Ed25519Signer()
    plugin_key = ActorKey(
        "plugin:planning.expand.v1",
        "planning-1",
        plugin_signer.kind,
        plugin_signer.signer_id,
        ("actor.authorize", "contract.publish", "worker.register"),
    )
    genesis = create_genesis_manifest(
        "runtime-multistep", (plugin_key,), "policy:runtime-multistep"
    )
    initialize_genesis(store, genesis)
    ingress = candidate_runtime(
        store, genesis=genesis, actor_key=plugin_key, signer=plugin_signer
    )
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="research_report",
            outputs=("final_report",),
            budget=5,
        )
    )
    worker = MockWorker(worker_id="worker")
    host = hosted_worker(
        store,
        worker,
        genesis=genesis,
        authority_key=plugin_key,
        authority_signer=plugin_signer,
    )

    worker.set_output("research_report", '/plan {"reason":"decompose"}')
    root_claim = claim_next_package(store, worker_id="worker", contract_id=root.id)
    assert root_claim is not None
    scheduled = execute_claimed_package(root_claim, host, store)
    assert scheduled["status"] == "task_delegated"
    children = [
        store.get_contract(child_id) for child_id in scheduled["child_contract_ids"]
    ]
    assert len(children) == 3
    assert all(child is not None for child in children)
    assert {
        label
        for child in children
        for label in child.labels
        if label.startswith("plugin:plan.")
    } == {
        "plugin:plan.draft",
        "plugin:plan.dependencies",
        "plugin:plan.compile",
    }
    assert not store.scan_runtime_records(record_type="task.delegated")
    assert not any(
        record.payload.get("contract_id") == root.id
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    )
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
    ingress = candidate_runtime(
        store, genesis=genesis, actor_key=plugin_key, signer=plugin_signer
    )
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="tool_report",
            outputs=("final_report",),
            tool_scope=("lookup",),
            budget=4,
        )
    )
    worker = MockWorker(worker_id="worker")
    host = WorkerHost(worker, genesis, worker_key, worker_signer)
    worker.set_output(
        root.name,
        '/tool {"name":"lookup","args":{"key":"x"}}',
    )
    root_claim = claim_next_package(store, worker_id="worker", contract_id=root.id)
    assert root_claim is not None
    scheduled = execute_claimed_package(root_claim, host, store)
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
    tool_view = RuntimeProjection(store, store).contract_view(tool_contract)
    assert tool_view.enabled, tool_view.blockers
    registration_view = store.get_worker_registration("worker")
    assert registration_view is not None
    assert is_eligible(tool_contract, registration_view)
    tool_claim = claim_next_package(
        store, worker_id="worker", contract_id=tool_contract.id
    )
    assert tool_claim is not None
    assert execute_claimed_package(tool_claim, host, store)["status"] == "accepted"

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
    assert len(continuations) == 1, (
        [(item.origin, item.name) for item in store.get_all_contracts()],
        [
            dict(record.payload)
            for _, record in store.scan_runtime_records()
            if record.record_type.endswith("rejected")
        ],
    )
    receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == plugin_key.actor_id
        and record.payload.get("effect_types") == ("contract.declare",)
    ]
    assert len(receipts) == 2


def test_fail_completion_plugin_closes_parent_and_publishes_report_candidate():
    store = SQLiteStore(":memory:")
    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:fail.report.v1",
        "fail-report-1",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest("runtime-fail", (actor,), "policy:runtime-fail")
    initialize_genesis(store, genesis)
    publisher = CandidatePublisher(store, store, genesis, actor, signer)
    ingress = candidate_runtime(store, genesis=genesis, actor_key=actor, signer=signer)
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="failing_task",
            outputs=("unreachable_output",),
            budget=3,
        )
    )
    worker = MockWorker(worker_id="worker")
    host = hosted_worker(
        store,
        worker,
        genesis=genesis,
        authority_key=actor,
        authority_signer=signer,
    )
    worker.set_output(root.name, '/fail {"reason":"source unavailable"}')
    root_claim = claim_next_package(store, worker_id="worker", contract_id=root.id)
    assert root_claim is not None
    scheduled = execute_claimed_package(root_claim, host, store)
    fail_contract = store.get_contract(scheduled["child_contract_id"])
    assert fail_contract is not None
    worker.set_output(
        fail_contract.name,
        "/exec "
        + json.dumps({fail_contract.outputs[0]: '{"reason":"source unavailable"}'}),
    )
    fail_claim = claim_next_package(
        store, worker_id="worker", contract_id=fail_contract.id
    )
    assert fail_claim is not None
    assert execute_claimed_package(fail_claim, host, store)["status"] == "accepted"

    processed = process_method_completions(
        store,
        default_completion_registry(),
        candidate_publishers=CandidatePublisherRegistry(
            (("fail.report.v1", publisher),)
        ),
    )

    assert processed == [fail_contract.id]
    root_view = RuntimeProjection(store, store).contract_view(root)
    assert root_view.terminal == "failed"
    assert root_view.enabled is False
    assert root_view.blockers == ("terminal:failed",)
    reports = store.get_assets_by_name(f"_fail_report_{fail_contract.id}")
    assert len(reports) == 1
    receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == actor.actor_id
        and record.payload.get("effect_types") == ("asset.propose",)
    ]
    assert len(receipts) == 1
