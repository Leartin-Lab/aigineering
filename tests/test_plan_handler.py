"""Tests for PlanMethodHandler (v0.3.4 plan-logic extraction)."""

import json

from aigineering.core.engine import Engine
from aigineering.core.ids import hash_asset_content, hash_contract
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_handlers.plan import PlanMethodHandler
from aigineering.core.methods import method_payload
from aigineering.core.store import MemoryStore
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Contract


class SequenceWorker:
    worker_id = "sequence_worker"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[Contract, list[Asset]]] = []

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]):
        from aigineering.protocol.types import Candidate

        self.calls.append((contract, disclosed_assets))
        raw_output = self._outputs.pop(0) if self._outputs else ""
        return Candidate(worker_id=self.worker_id, raw_output=raw_output)


# ── Handler unit tests ────────────────────────────────────────────────

def test_handler_can_handle_plan():
    handler = PlanMethodHandler()
    assert handler.can_handle("plan") is True
    assert handler.can_handle("tool") is False
    assert handler.can_handle("replan") is False


def test_handler_schedules_plan_child():
    """PlanMethodHandler.handle_method creates a child contract via scheduler."""
    registry = MethodRegistry()
    handler = PlanMethodHandler()
    registry.register("plan", handler)

    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(['/plan {"reason": "split work"}', ""])

    contract = Contract(
        id=hash_contract("root", "", [], ["report"], "", 5, [], [], "human"),
        name="root",
        inputs=[],
        outputs=["report"],
        activation="",
        budget=5,
    )

    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    child_contracts = [
        c for c in store.get_all_contracts() if c.parent_id == contract.id
    ]
    assert len(child_contracts) == 1
    assert child_contracts[0].name == "root.plan"
    assert child_contracts[0].origin == "system"


def test_handler_does_not_expand_non_plan():
    """handle_completion returns False for non-plan method contracts."""
    handler = PlanMethodHandler()

    from aigineering.core.methods import method_payload

    store = MemoryStore()
    trace_store = TraceStore()

    class MinimalEngine:
        _store = store
        _trace = trace_store
        _budget: dict[str, int] = {}

        def _add_trace(self, *args, **kwargs):
            pass

        def _resolve_budget(self, contract):
            return 1

        def add_contract(self, contract):
            store.add_contract(contract)

    engine = MinimalEngine()
    tool_contract = Contract(
        id="tool_child_1",
        parent_id="parent_1",
        name="parent.tool",
        description=json.dumps(
            {"method": "tool", "parent_contract_id": "parent_1",
             "parent_contract_name": "parent", "payload": {}},
            sort_keys=True,
        ),
        inputs=[],
        outputs=["_tool_obs_tool_child_1"],
        activation="_method_ctx_parent_1",
        budget=1,
        origin="system",
    )

    result = handler.handle_completion(engine, tool_contract, [])
    assert result is False


# ── Engine integration tests ──────────────────────────────────────────

def test_handler_expands_plan_results():
    """Full engine flow: PlanMethodHandler expands plan results into children."""
    registry = MethodRegistry()
    handler = PlanMethodHandler()
    registry.register("plan", handler)

    plan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft",
                    "description": "Draft the report.",
                    "inputs": ["source"],
                    "outputs": ["draft_report"],
                    "activation": "source",
                    "budget": 2,
                    "tool_scope": ["lookup"],
                    "labels": ["research"],
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(plan_content)}}}}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
        labels=["research"],
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    planned = [
        c for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned) == 1
    assert planned[0].origin == "plan"
    assert planned[0].outputs == ("draft_report",)
    assert planned[0].tool_scope == ("lookup",)
    assert planned[0].labels == ("research",)

    expanded = trace_store.get_by_event_type("contracts_expanded")
    assert len(expanded) == 1
    assert expanded[0].relation_type == "plan"
    assert expanded[0].relation_target == planned[0].id


def test_handler_respects_containment():
    """PlanMethodHandler rejects tool-scope escalation in child contracts."""
    registry = MethodRegistry()
    handler = PlanMethodHandler()
    registry.register("plan", handler)

    plan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft",
                    "outputs": ["draft_report"],
                    "budget": 1,
                    "tool_scope": ["lookup", "write"],
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(plan_content)}}}}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    planned = [
        c for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned) == 1
    assert planned[0].tool_scope == ("lookup",)

    rejections = trace_store.get_by_event_type("containment_rejected")
    assert len(rejections) >= 1
    clamp_events = [r for r in rejections if r.authority_result == "clamped"]
    assert len(clamp_events) >= 1


def test_engine_uses_plan_handler():
    """Full engine test: registered PlanMethodHandler drives plan expansion."""
    registry = MethodRegistry()
    handler = PlanMethodHandler()
    registry.register("plan", handler)

    plan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "research",
                    "description": "Research phase",
                    "inputs": [],
                    "outputs": ["raw_data"],
                    "activation": "",
                    "budget": 3,
                    "tool_scope": [],
                    "labels": [],
                },
                {
                    "name": "write",
                    "description": "Write phase",
                    "inputs": ["raw_data"],
                    "outputs": ["final_report"],
                    "activation": "raw_data",
                    "budget": 2,
                    "tool_scope": [],
                    "labels": [],
                },
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "two-phase"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(plan_content)}}}}}',
            "",
            "",
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["final_report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    research_children = [
        c for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "research"
    ]
    write_children = [
        c for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "write"
    ]
    assert len(research_children) == 1, "research child should be created"
    assert len(write_children) == 1, "write child should be created"

    expanded = trace_store.get_by_event_type("contracts_expanded")
    assert len(expanded) == 1
    created_ids = expanded[0].relation_target.split(",")
    assert len(created_ids) == 2
    assert research_children[0].id in created_ids
    assert write_children[0].id in created_ids


def test_fallback_without_handler():
    """Engine works without PlanMethodHandler (backward compat)."""
    plan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft",
                    "outputs": ["draft_report"],
                    "budget": 1,
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(plan_content)}}}}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.run()

    planned = [
        c for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned) == 1
    assert planned[0].origin == "plan"

    expanded = trace_store.get_by_event_type("contracts_expanded")
    assert len(expanded) == 1


def test_handler_satisfies_protocol():
    """PlanMethodHandler is structurally compatible with MethodHandler."""
    from aigineering.core.method_registry import MethodHandler

    handler = PlanMethodHandler()
    # Structural protocol check: all required methods are present
    assert hasattr(handler, "can_handle")
    assert hasattr(handler, "handle_method")
    assert hasattr(handler, "handle_completion")
    assert callable(handler.can_handle)
    assert callable(handler.handle_method)
    assert callable(handler.handle_completion)
