"""End-to-end multi-step planning through the stateless worker protocol."""

from __future__ import annotations

import json

from aigineering.agent.mock import MockWorker
from aigineering.application import default_method_registry
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    process_method_completions,
)


def test_plan_method_and_independent_child_complete_root_from_assets():
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    root = ingress.accept_contract(
        build_control_plane_contract(
            name="research_report",
            outputs=("final_report",),
            budget=5,
        )
    )
    registry = default_method_registry()
    worker = MockWorker()

    worker.set_output("research_report", '/plan {"reason":"decompose"}')
    root_claim = claim_next_package(store, worker_id="worker", contract_id=root.id)
    assert root_claim is not None
    scheduled = execute_claimed_package(
        root_claim, worker, store, method_registry=registry
    )
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
    plan_result = execute_claimed_package(
        plan_claim, worker, store, method_registry=registry
    )
    assert plan_result["status"] == "accepted"

    assert process_method_completions(store, registry) == [plan_contract.id]
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
    child_result = execute_claimed_package(
        child_claim, worker, store, method_registry=registry
    )
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
