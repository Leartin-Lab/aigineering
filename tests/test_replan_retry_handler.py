"""Tests for ReplanMethodHandler and RetryMethodHandler (v0.4.7)."""

import json

from aigineering.core.engine import Engine
from aigineering.core.ids import hash_asset_content, hash_contract, hash_retry
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_handlers.replan import ReplanMethodHandler
from aigineering.core.method_handlers.retry import RetryMethodHandler
from aigineering.core.method_runtime import MethodRuntime
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


# ── ReplanMethodHandler ────────────────────────────────────────────────


def test_replan_handler_can_handle():
    handler = ReplanMethodHandler()
    assert handler.can_handle("replan") is True
    assert handler.can_handle("plan") is False
    assert handler.can_handle("tool") is False
    assert handler.can_handle("retry") is False


def test_replan_handler_schedules_child():
    """ReplanMethodHandler.handle_method creates a child contract via scheduler."""
    registry = MethodRegistry()
    handler = ReplanMethodHandler()
    registry.register("replan", handler)

    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(['/replan {"reason": "try again"}', ""])

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
    assert child_contracts[0].name == "root.replan"
    assert child_contracts[0].origin == "system"

    # Check trace event
    method_scheduled = trace_store.get_by_event_type("method_scheduled")
    assert len(method_scheduled) == 1
    assert method_scheduled[0].relation_type == "replan"


def test_replan_handler_does_not_expand_non_replan():
    """handle_completion returns False for non-replan method contracts."""
    handler = ReplanMethodHandler()

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
    plan_contract = Contract(
        id="plan_child_1",
        parent_id="parent_1",
        name="parent.plan",
        description=json.dumps(
            {
                "method": "plan",
                "parent_contract_id": "parent_1",
                "parent_contract_name": "parent",
                "payload": {},
            },
            sort_keys=True,
        ),
        inputs=[],
        outputs=["_plan_result_parent_1"],
        activation="_method_ctx_parent_1",
        budget=1,
        origin="system",
    )

    result = handler.handle_completion(engine, plan_contract, [])
    assert result is False


def test_replan_handler_expands_replan_results():
    """Full engine flow: ReplanMethodHandler expands replan results into children."""
    registry = MethodRegistry()
    handler = ReplanMethodHandler()
    registry.register("replan", handler)

    replan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "revised_draft",
                    "description": "Revised draft phase.",
                    "inputs": ["source"],
                    "outputs": ["revised_report"],
                    "activation": "source",
                    "budget": 3,
                    "tool_scope": ["lookup"],
                    "labels": ["research"],
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/replan {"reason": "revise plan"}',
            f'/exec {{"outputs": {{"_replan_result_contract_parent": {json.dumps(replan_content)}}}}}',
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
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "revised_draft"
    ]
    assert len(planned) == 1
    assert planned[0].origin == "plan"
    assert planned[0].outputs == ("revised_report",)
    assert planned[0].tool_scope == ("lookup",)
    assert planned[0].labels == ("research",)

    expanded = trace_store.get_by_event_type("contracts_expanded")
    assert len(expanded) == 1
    assert expanded[0].relation_type == "replan"
    assert expanded[0].relation_target == planned[0].id


def test_replan_handler_satisfies_protocol():
    """ReplanMethodHandler is structurally compatible with MethodHandler."""
    handler = ReplanMethodHandler()
    assert hasattr(handler, "can_handle")
    assert hasattr(handler, "handle_method")
    assert hasattr(handler, "handle_completion")
    assert callable(handler.can_handle)
    assert callable(handler.handle_method)
    assert callable(handler.handle_completion)


# ── RetryMethodHandler ─────────────────────────────────────────────────


def test_retry_handler_can_handle():
    handler = RetryMethodHandler()
    assert handler.can_handle("retry") is True
    assert handler.can_handle("plan") is False
    assert handler.can_handle("replan") is False
    assert handler.can_handle("tool") is False


def test_retry_creates_deterministic_contract():
    """RetryMethodHandler creates a retry contract with deterministic ID."""
    registry = MethodRegistry()
    handler = RetryMethodHandler()
    registry.register("retry", handler)

    store = MemoryStore()
    trace_store = TraceStore()

    contract_id = hash_contract("root", "", [], ["report"], "", 5, [], [], "human")
    contract = Contract(
        id=contract_id,
        name="root",
        inputs=["data"],
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
        labels=["research"],
    )

    worker = SequenceWorker(['/retry {"reason": "failed, try again"}', ""])
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    # Verify deterministic ID
    expected_retry_id = hash_retry(contract_id)
    retry_contract = store.get_contract(expected_retry_id)
    assert retry_contract is not None, f"Retry contract {expected_retry_id} not found"
    assert retry_contract.name == contract.name
    assert retry_contract.inputs == contract.inputs
    assert retry_contract.outputs == contract.outputs
    assert retry_contract.budget == contract.budget
    assert retry_contract.tool_scope == contract.tool_scope
    assert retry_contract.labels == contract.labels
    assert retry_contract.origin == contract.origin
    assert retry_contract.parent_id == contract.parent_id

    # Verify trace event
    retry_events = trace_store.get_by_event_type("retry_created")
    assert len(retry_events) == 1
    assert retry_events[0].relation_type == "retry"
    assert retry_events[0].relation_target == expected_retry_id

    # Verify parent is suspended
    assert contract.id in engine._suspended


def test_retry_handler_idempotent():
    """RetryMethodHandler does not create duplicate retry contracts for the same parent."""
    registry = MethodRegistry()
    handler = RetryMethodHandler()
    registry.register("retry", handler)

    store = MemoryStore()
    trace_store = TraceStore()

    contract_id = hash_contract("root2", "", [], ["report"], "", 5, [], [], "human")
    contract = Contract(
        id=contract_id,
        name="root2",
        inputs=[],
        outputs=["report"],
        activation="",
        budget=5,
    )

    from aigineering.protocol.types import Candidate

    runtime = MethodRuntime(store, trace_store, {contract_id: 5})

    candidate = Candidate(worker_id="test", raw_output='/retry {"reason": "first"}')

    # First call — creates retry contract
    result1 = handler.handle_method(runtime, contract, "retry", candidate)
    assert result1 is True
    expected_retry_id = hash_retry(contract_id)
    assert store.get_contract(expected_retry_id) is not None

    # Second call — idempotent, already exists
    result2 = handler.handle_method(runtime, contract, "retry", candidate)
    assert result2 is True
    assert store.get_contract(expected_retry_id) is not None

    # Only one contract created with that ID
    all_contracts = store.get_all_contracts()
    assert len(all_contracts) == 1


def test_retry_handler_completion_returns_false():
    """Retry handler has no method sub-contract to complete."""
    handler = RetryMethodHandler()
    result = handler.handle_completion(None, None, [])
    assert result is False


# ── Context overflow → replan ──────────────────────────────────────────


def test_context_overflow_triggers_replan():
    """When scope assets exceed context_size_limit, the engine triggers replan."""
    registry = MethodRegistry()
    replan_handler = ReplanMethodHandler()
    registry.register("replan", replan_handler)

    store = MemoryStore()
    trace_store = TraceStore()

    # Create large disclosure assets to exceed the limit (500 chars limit)
    large_content = "x" * 3000  # ~750 token estimate
    large_asset = Asset(
        id=hash_asset_content("large_data", large_content),
        name="large_data",
        content=large_content,
    )

    # Limit set to 1000 tokens (4000 chars) — large_content is 3000 chars
    contract_id = hash_contract(
        "ctx_root", "", ["large_data"], ["report"], "large_data", 5, [], [], "human"
    )
    contract = Contract(
        id=contract_id,
        name="ctx_root",
        inputs=["large_data"],
        outputs=["report"],
        activation="large_data",
        budget=5,
    )

    # Worker should not be called — context overflow intercepts before invoke
    worker = SequenceWorker([""])
    engine = Engine(
        store, worker, trace_store, method_registry=registry, context_size_limit=500
    )
    engine.add_contract(contract)
    engine.add_asset(large_asset)
    engine.run()

    # Verify context_overflow trace event was emitted
    overflow_events = trace_store.get_by_event_type("context_overflow")
    assert len(overflow_events) == 1
    assert overflow_events[0].relation_type == "replan"
    assert overflow_events[0].relation_target == "context_size_exceeded"

    # Verify replan sub-contract was scheduled (via handler)
    child_contracts = [
        c for c in store.get_all_contracts() if c.parent_id == contract.id
    ]
    assert len(child_contracts) >= 1
    assert any(c.name == "ctx_root.replan" for c in child_contracts)


def test_context_overflow_within_limit_does_not_trigger():
    """When scope is within limit, normal execution proceeds."""
    registry = MethodRegistry()
    store = MemoryStore()
    trace_store = TraceStore()

    small_content = "short data"
    small_asset = Asset(
        id=hash_asset_content("small_data", small_content),
        name="small_data",
        content=small_content,
    )

    contract_id = hash_contract(
        "small_root", "", ["small_data"], ["report"], "small_data", 5, [], [], "human"
    )
    contract = Contract(
        id=contract_id,
        name="small_root",
        inputs=["small_data"],
        outputs=["report"],
        activation="small_data",
        budget=5,
    )

    worker = SequenceWorker(['/exec {"outputs": {"report": "done"}}', ""])
    engine = Engine(
        store, worker, trace_store, method_registry=registry, context_size_limit=500
    )
    engine.add_contract(contract)
    engine.add_asset(small_asset)
    engine.run()

    # No context_overflow trace
    overflow_events = trace_store.get_by_event_type("context_overflow")
    assert len(overflow_events) == 0

    # Normal execution happened
    assert len(worker.calls) >= 1


def test_context_overflow_no_limit_does_not_trigger():
    """When context_size_limit is None (default), no overflow check occurs."""
    registry = MethodRegistry()
    store = MemoryStore()
    trace_store = TraceStore()

    large_content = "x" * 10000
    large_asset = Asset(
        id=hash_asset_content("big_data", large_content),
        name="big_data",
        content=large_content,
    )

    contract_id = hash_contract(
        "big_root", "", ["big_data"], ["report"], "big_data", 5, [], [], "human"
    )
    contract = Contract(
        id=contract_id,
        name="big_root",
        inputs=["big_data"],
        outputs=["report"],
        activation="big_data",
        budget=5,
    )

    worker = SequenceWorker(['/exec {"outputs": {"report": "done"}}', ""])
    engine = Engine(store, worker, trace_store, method_registry=registry)
    engine.add_contract(contract)
    engine.add_asset(large_asset)
    engine.run()

    # No context_overflow trace (limit is None)
    overflow_events = trace_store.get_by_event_type("context_overflow")
    assert len(overflow_events) == 0

    # Worker was still invoked normally
    assert len(worker.calls) >= 1
