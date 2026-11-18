"""End-to-end multi-step planning through the stateless worker protocol."""

from __future__ import annotations

import json
import pytest
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerHost
from aigineering.core.candidate_publisher import CandidatePublisher
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
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
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


def test_tool_scope_permission_allows_output_but_underfunded_call_is_observable():
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    worker = MockWorker(worker_id="scope-worker")
    host = hosted_worker(
        store,
        worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )

    direct = setup.accept_contract(
        build_control_plane_contract(
            name="scoped-direct-output",
            outputs=("report",),
            budget=1,
            tool_scope=("lookup",),
        )
    )
    worker.set_output(
        direct.name,
        '/exec {"outputs":{"report":"recovery result"}}',
    )
    direct_claim = claim_next_package(
        store, worker_id=worker.worker_id, contract_id=direct.id
    )
    assert direct_claim is not None
    direct_result = execute_claimed_package(direct_claim, host, store)
    assert direct_result["status"] == "accepted"
    assert [asset.content for asset in store.get_assets_by_name("report")] == [
        "recovery result"
    ]

    underfunded = setup.accept_contract(
        build_control_plane_contract(
            name="scoped-underfunded-tool",
            outputs=("other_report",),
            budget=1,
            tool_scope=("lookup",),
        )
    )
    worker.set_output(
        underfunded.name,
        '/tool {"name":"lookup","args":{"query":"distinct"}}',
    )
    tool_claim = claim_next_package(
        store, worker_id=worker.worker_id, contract_id=underfunded.id
    )
    assert tool_claim is not None
    with pytest.raises(WorkerInvocationError, match="worker invocation failed"):
        execute_claimed_package(tool_claim, host, store)
    failures = store.scan_runtime_records(record_type="worker.invocation_failed")
    assert failures[-1][1].payload["category"] == (
        "worker_error:task_delegation_rejected"
    )
    assert "lacks allowance" in failures[-1][1].payload["diagnostic"]
    assert store.get_assets_by_name("other_report") == []
    store.close()


def test_negative_tool_observation_uses_predeclared_ordinary_continuations(
    tmp_path,
):
    db_path = tmp_path / "ordinary-tool-continuations.db"
    store = SQLiteStore(str(db_path))
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
        "plugin:test-ordinary-continuations",
        "ordinary-continuations-1",
        plugin_signer.kind,
        plugin_signer.signer_id,
        (
            "actor.authorize",
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
                    capabilities=("tool-execution", "tool:lookup"),
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
    assert registration.accepted
    ingress = candidate_runtime(
        store, genesis=genesis, actor_key=plugin_key, signer=plugin_signer
    )
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="tool_report",
            outputs=("final_report",),
            tool_scope=("lookup",),
            worker_capabilities=("tool-execution",),
            budget=8,
        )
    )
    worker = MockWorker(worker_id="worker")
    host = WorkerHost(worker, genesis, worker_key, worker_signer)
    worker.set_output(root.name, '/tool {"name":"lookup","args":{"key":"x"}}')
    root_claim = claim_next_package(store, worker_id="worker", contract_id=root.id)
    assert root_claim is not None
    delegated = execute_claimed_package(root_claim, host, store)
    assert delegated["status"] == "task_delegated"
    children = [store.get_contract(item) for item in delegated["child_contract_ids"]]
    tool_contract = next(
        item for item in children if item is not None and "plugin:tool" in item.labels
    )
    continuation = next(
        item for item in children if item is not None and item.origin == "continuation"
    )
    assert continuation.inputs[-1] == tool_contract.outputs[0]
    assert continuation.activation == tool_contract.outputs[0]
    assert continuation.tool_scope == root.tool_scope

    observation = json.dumps(
        {"ok": False, "tool": "lookup", "result": "", "error": "fixture"},
        sort_keys=True,
    )
    worker.set_output(
        tool_contract.name,
        "/exec " + json.dumps({tool_contract.outputs[0]: observation}),
    )
    assert RuntimeProjection(store, store).contract_view(tool_contract).enabled
    registration_view = store.get_worker_registration("worker")
    assert registration_view is not None and is_eligible(
        tool_contract, registration_view
    )
    tool_claim = claim_next_package(
        store, worker_id="worker", contract_id=tool_contract.id
    )
    assert tool_claim is not None
    assert execute_claimed_package(tool_claim, host, store)["status"] == "accepted"
    assert store.get_assets_by_name(tool_contract.outputs[0])[0].content == observation
    assert RuntimeProjection(store, store).contract_view(continuation).enabled
    assert store.get_assets_by_name("final_report") == []
    assert RuntimeProjection(store, store).contract_view(root).terminal is None

    worker.set_output(
        continuation.name, '/tool {"name":"lookup","args":{"key":"second"}}'
    )
    continuation_claim = claim_next_package(
        store, worker_id="worker", contract_id=continuation.id
    )
    assert continuation_claim is not None
    repeated = execute_claimed_package(continuation_claim, host, store)
    assert repeated["status"] == "task_delegated"
    repeated_children = [
        store.get_contract(item) for item in repeated["child_contract_ids"]
    ]
    repeated_tool = next(
        item
        for item in repeated_children
        if item is not None and "plugin:tool" in item.labels
    )
    repeated_continuation = next(
        item
        for item in repeated_children
        if item is not None and item.origin == "continuation"
    )
    assert repeated_tool.parent_id == continuation.id
    assert repeated_tool.tool_scope == continuation.tool_scope
    assert repeated_continuation.activation == repeated_tool.outputs[0]
    assert repeated_continuation.inputs[-1] == repeated_tool.outputs[0]

    repeated_observation = json.dumps(
        {"ok": True, "tool": "lookup", "result": "second", "error": None},
        sort_keys=True,
    )
    worker.set_output(
        repeated_tool.name,
        "/exec " + json.dumps({repeated_tool.outputs[0]: repeated_observation}),
    )
    repeated_claim = claim_next_package(
        store, worker_id="worker", contract_id=repeated_tool.id
    )
    assert repeated_claim is not None
    assert execute_claimed_package(repeated_claim, host, store)["status"] == "accepted"
    assert [
        asset.content
        for asset in store.get_assets_by_name(repeated_tool.outputs[0])
        if asset.created_by == repeated_tool.id
    ] == [repeated_observation]
    assert store.get_assets_by_name("final_report") == []
    before_digest = store.runtime_materialization_digest()
    store.close()

    reopened = SQLiteStore(str(db_path))
    try:
        assert reopened.rebuild_runtime_materializations() == before_digest
        assert (
            len(
                [
                    item
                    for item in reopened.get_all_contracts()
                    if item.parent_id in {root.id, continuation.id}
                    and item.origin == "continuation"
                ]
            )
            == 2
        )
        assert (
            RuntimeProjection(reopened, reopened).contract_view(root).terminal is None
        )
        assert reopened.get_assets_by_name("final_report") == []
    finally:
        reopened.close()


def test_fail_action_report_does_not_complete_unsatisfied_parent():
    store = SQLiteStore(":memory:")
    ingress = candidate_runtime(store)
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="failing_task", outputs=("unreachable_output",), budget=3
        )
    )
    worker = MockWorker(worker_id="worker:fail-report")
    host = hosted_worker(
        store,
        worker,
        genesis=ingress.genesis,
        authority_key=ingress.actor_key,
        authority_signer=ingress.signer,
    )
    worker.set_output(root.name, '/fail {"reason":"source unavailable"}')
    root_claim = claim_next_package(
        store, worker_id=worker.worker_id, contract_id=root.id
    )
    assert root_claim is not None
    delegated = execute_claimed_package(root_claim, host, store)
    assert delegated["status"] == "task_delegated"
    report_contract = store.get_contract(delegated["child_contract_ids"][0])
    assert report_contract is not None
    worker.set_output(
        report_contract.name,
        "/exec "
        + json.dumps({report_contract.outputs[0]: '{"reason":"source unavailable"}'}),
    )
    report_claim = claim_next_package(
        store, worker_id=worker.worker_id, contract_id=report_contract.id
    )
    assert report_claim is not None
    assert execute_claimed_package(report_claim, host, store)["status"] == "accepted"

    root_view = RuntimeProjection(store, store).contract_view(root)
    assert root_view.terminal is None
    assert root_view.outputs_satisfied is False
    assert store.get_assets_by_name(root.outputs[0]) == []
    assert store.get_assets_by_name(report_contract.outputs[0])
    store.close()
