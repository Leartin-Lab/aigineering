"""Tests for PlanMethodHandler (v0.3.4 plan-logic extraction)."""

import json

from aigineering.core.engine import Engine
from aigineering.core.ids import hash_contract
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_handlers.plan import PlanMethodHandler
from aigineering.core.store import MemoryStore
from aigineering.core.trace import TraceStore
from aigineering.protocol.actions import parse_method_action
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
    contract_id = hash_contract("root", "", [], ["report"], "", 5, [], [], "human")
    empty_plan = json.dumps({"contracts": []}, sort_keys=True)
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_{contract_id}": {json.dumps(empty_plan)}}}}}',
        ]
    )

    contract = Contract(
        id=contract_id,
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

    context_assets = store.get_assets_by_name(f"_method_ctx_{contract.id}")
    assert len(context_assets) == 1
    context = json.loads(context_assets[0].content)
    assert context == {
        "method": "plan",
        "parent_contract_id": contract.id,
        "child_contract_id": child_contracts[0].id,
        "payload": {"reason": "split work"},
    }


def test_handler_does_not_expand_non_plan():
    """handle_completion returns False for non-plan method contracts."""
    handler = PlanMethodHandler()

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
            {
                "method": "tool",
                "parent_contract_id": "parent_1",
                "parent_contract_name": "parent",
                "payload": {},
            },
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
        inputs=["source"],
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
        labels=["research"],
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.add_asset(Asset(id="asset_source", name="source", content="observed"))
    engine.run()

    planned = [
        c
        for c in store.get_all_contracts()
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


def test_malformed_plan_result_schedules_recovery_and_expands_repaired_result():
    """Rejected plan-result schema is returned to a new Worker recovery task."""
    registry = MethodRegistry()
    registry.register("plan", PlanMethodHandler())

    malformed_plan = json.dumps(
        {
            "plan_name": "wrong_shape",
            "child_contracts": [
                {
                    "contract_name": "draft",
                    "expected_outputs": ["draft_report"],
                }
            ],
        },
        sort_keys=True,
    )
    repaired_plan = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft",
                    "description": "Draft the report.",
                    "inputs": ["source"],
                    "outputs": ["draft_report"],
                    "activation": "source",
                    "budget": 1,
                    "tool_scope": [],
                    "labels": [],
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(malformed_plan)}}}}}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(repaired_plan)}}}}}',
            '/exec {"outputs": {"draft_report": "draft content"}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        inputs=["source"],
        outputs=["report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.add_asset(Asset(id="asset_source", name="source", content="observed"))
    engine.run()

    recovery_contracts = [
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name.endswith(".recover")
    ]
    assert len(recovery_contracts) == 1
    assert recovery_contracts[0].outputs == ("_plan_result_contract_parent",)

    failure_contexts = [
        a for a in store.get_all_assets() if a.name.startswith("_fail_context_")
    ]
    assert len(failure_contexts) == 1
    assert failure_contexts[0].name in recovery_contracts[0].inputs

    planned = [
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned) == 1

    recovery_events = trace_store.get_by_event_type("method_recovery_scheduled")
    assert len(recovery_events) == 1
    assert recovery_events[0].relation_target == recovery_contracts[0].id


def test_mixed_plan_rejection_is_atomic_and_schedules_recovery():
    registry = MethodRegistry()
    registry.register("plan", PlanMethodHandler())
    mixed_plan = json.dumps(
        {
            "contracts": [
                {
                    "name": "premature",
                    "inputs": ["source"],
                    "outputs": ["notes"],
                    "activation": "source",
                    "budget": 1,
                },
                {
                    "name": "invalid",
                    "inputs": ["notes", "source"],
                    "outputs": ["report"],
                    "activation": "notes, source",
                    "budget": 1,
                },
            ]
        },
        sort_keys=True,
    )
    repaired_plan = json.dumps(
        {
            "contracts": [
                {
                    "name": "final",
                    "inputs": ["source"],
                    "outputs": ["report"],
                    "activation": "source",
                    "budget": 1,
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(mixed_plan)}}}}}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(repaired_plan)}}}}}',
            '/exec {"outputs": {"report": "done"}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        inputs=["source"],
        outputs=["report"],
        budget=5,
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.add_asset(Asset(id="asset_source", name="source", content="observed"))

    engine.run()

    names = {child.name for child in store.get_all_contracts()}
    assert "premature" not in names
    assert "final" in names
    assert "root.plan.recover" in names
    assert trace_store.get_by_event_type("method_recovery_scheduled")


def test_plan_method_action_schedules_recovery_instead_of_nested_plan():
    """A .plan task returning /plan is repaired instead of nested indefinitely."""
    registry = MethodRegistry()
    registry.register("plan", PlanMethodHandler())

    repaired_plan = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft",
                    "description": "Draft the report.",
                    "inputs": ["source"],
                    "outputs": ["draft_report"],
                    "activation": "source",
                    "budget": 1,
                    "tool_scope": [],
                    "labels": [],
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            '/plan {"reason": "I need to plan the plan"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(repaired_plan)}}}}}',
            '/exec {"outputs": {"draft_report": "draft content"}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        inputs=["source"],
        outputs=["report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.add_asset(Asset(id="asset_source", name="source", content="observed"))
    engine.run()

    nested_plan_contracts = [
        c for c in store.get_all_contracts() if c.name == "root.plan.plan"
    ]
    assert nested_plan_contracts == []

    recovery_contracts = [
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "root.plan.recover"
    ]
    assert len(recovery_contracts) == 1

    planned = [
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned) == 1

    failed = [
        entry
        for entry in trace_store.get_by_event_type("failed")
        if entry.relation_type == "plan"
    ]
    assert len(failed) == 1
    assert failed[0].relation_target == "plan"


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
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned) == 0

    rejections = trace_store.get_by_event_type("containment_rejected")
    assert len(rejections) >= 1
    reject_events = [r for r in rejections if r.authority_result == "rejected"]
    assert len(reject_events) >= 1


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
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "research"
    ]
    write_children = [
        c
        for c in store.get_all_contracts()
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


def test_plan_completion_without_handler_fails_closed():
    """Engine must not expand plan results without a registered handler."""
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
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert planned == []

    expanded = trace_store.get_by_event_type("contracts_expanded")
    assert expanded == []
    missing = trace_store.get_by_event_type("method_handler_missing")
    assert len(missing) == 1
    assert missing[0].authority_result == "rejected"
    assert missing[0].relation_type == "plan"


def test_handler_satisfies_protocol():
    """PlanMethodHandler is structurally compatible with MethodHandler."""

    handler = PlanMethodHandler()
    # Structural protocol check: all required methods are present
    assert hasattr(handler, "can_handle")
    assert hasattr(handler, "handle_method")
    assert hasattr(handler, "handle_completion")
    assert callable(handler.can_handle)
    assert callable(handler.handle_method)
    assert callable(handler.handle_completion)


def test_legacy_handler_without_handle_completion_fails_closed():
    """A handler without handle_completion must not trigger Engine fallback."""
    registry = MethodRegistry()

    class LegacyHandler:
        def can_handle(self, action_type: str) -> bool:
            return action_type == "plan"

        def handle_method(self, runtime, contract, action_type, candidate) -> bool:
            runtime.schedule_method(contract, parse_method_action(candidate), candidate)
            return True

    registry.register("plan", LegacyHandler())

    plan_content = json.dumps(
        {"contracts": [{"name": "draft", "inputs": [], "outputs": ["draft_out"]}]},
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split"}',
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
        tool_scope=[],
        labels=[],
    )
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.run()
    children = [c for c in store.get_all_contracts() if c.parent_id == contract.id]
    draft = [c for c in children if c.name == "draft"]
    assert draft == []
    missing = trace_store.get_by_event_type("method_handler_missing")
    assert len(missing) == 1
    assert missing[0].authority_result == "rejected"
