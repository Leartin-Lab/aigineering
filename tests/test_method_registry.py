"""Tests for MethodHandler protocol and MethodRegistry dispatch."""

from aigineering.core.engine import Engine
from aigineering.core.ids import hash_asset_content, hash_contract
from aigineering.core.method_registry import MethodHandler, MethodRegistry
from aigineering.core.store import MemoryStore
from aigineering.core.tools import ToolRegistry
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Contract, ToolSpec


# ── SequenceWorker helper ────────────────────────────────────────────

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


# ── Recording handler for dispatch verification ─────────────────────

class _RecordingHandler:
    """Handler that records handle_method and can_handle calls."""

    def __init__(self, action_type: str) -> None:
        self.action_type = action_type
        self.handle_calls: list[tuple] = []
        self.can_handle_calls: list[str] = []

    def can_handle(self, action_type: str) -> bool:
        self.can_handle_calls.append(action_type)
        return action_type == self.action_type

    def handle_method(self, engine, contract, action_type, candidate) -> bool:
        self.handle_calls.append((engine, contract, action_type, candidate))
        return True


class _FalseHandler:
    """Handler whose can_handle always returns False."""

    def can_handle(self, action_type: str) -> bool:
        return False

    def handle_method(self, engine, contract, action_type, candidate) -> bool:
        return False


# ── Registry unit tests ─────────────────────────────────────────────

def test_register_and_get_handler():
    """Register a handler and retrieve it by action type."""
    registry = MethodRegistry()
    handler = _RecordingHandler("plan")

    registry.register("plan", handler)
    assert registry.get("plan") is handler


def test_register_overwrites():
    """Registering a second handler for the same type overwrites the first."""
    registry = MethodRegistry()
    handler1 = _RecordingHandler("plan")
    handler2 = _RecordingHandler("plan")

    registry.register("plan", handler1)
    registry.register("plan", handler2)

    assert registry.get("plan") is handler2


def test_deregister_removes_handler():
    """Deregister removes the handler — get returns None afterward."""
    registry = MethodRegistry()
    handler = _RecordingHandler("plan")
    registry.register("plan", handler)

    registry.deregister("plan")
    assert registry.get("plan") is None


def test_deregister_unknown_is_noop():
    """Deregister of an unregistered type does not raise."""
    registry = MethodRegistry()
    registry.deregister("nonexistent")  # must not raise


def test_list_types_returns_registered():
    """list_types returns the sorted list of registered action types."""
    registry = MethodRegistry()
    registry.register("tool", _RecordingHandler("tool"))
    registry.register("plan", _RecordingHandler("plan"))

    assert registry.list_types() == ["plan", "tool"]


def test_list_types_empty_initially():
    """list_types returns empty list when nothing is registered."""
    registry = MethodRegistry()
    assert registry.list_types() == []


def test_get_unknown_returns_none():
    """get for an unregistered type returns None."""
    registry = MethodRegistry()
    assert registry.get("plan") is None


def test_can_handle_on_registered_handler():
    """can_handle returns True for the matching type."""
    registry = MethodRegistry()
    handler = _RecordingHandler("plan")
    registry.register("plan", handler)

    retrieved = registry.get("plan")
    assert retrieved is not None
    assert retrieved.can_handle("plan") is True
    assert retrieved.can_handle("tool") is False


def test_handler_protocol_is_structural():
    """A class matching the MethodHandler protocol is accepted without inheritance."""

    class AdHocHandler:
        def can_handle(self, action_type: str) -> bool:
            return action_type == "custom"

        def handle_method(self, engine, contract, action_type, candidate) -> bool:
            return True

    registry = MethodRegistry()
    registry.register("custom", AdHocHandler())
    assert registry.get("custom") is not None


# ── Engine dispatch integration tests ───────────────────────────────

def test_engine_dispatches_to_registry():
    """Engine with registry calls handler.handle_method for method actions."""
    registry = MethodRegistry()
    handler = _RecordingHandler("plan")
    registry.register("plan", handler)

    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(['/plan {"reason": "test dispatch"}', ""])

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

    # Handler must have been consulted
    assert len(handler.can_handle_calls) >= 1
    assert handler.can_handle_calls[0] == "plan"

    # handle_method must have been called exactly once
    assert len(handler.handle_calls) == 1
    _, called_contract, called_action_type, called_candidate = handler.handle_calls[0]
    assert called_contract.id == contract.id
    assert called_action_type == "plan"

    # Engine still does its lifecycle — parent is suspended
    assert contract.id in engine._suspended

    # A child contract was created (default scheduling still runs in v0.3.3)
    child_contracts = [c for c in store.get_all_contracts() if c.parent_id == contract.id]
    assert len(child_contracts) == 1
    assert child_contracts[0].name == "root.plan"


def test_engine_falls_back_without_registry():
    """Engine without a registry works exactly as before (backward compat)."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(['/plan {"reason": "no registry"}', ""])

    contract = Contract(
        id=hash_contract("root", "", [], ["report"], "", 5, [], [], "human"),
        name="root",
        inputs=[],
        outputs=["report"],
        activation="",
        budget=5,
    )

    engine = Engine(store, worker, trace_store)  # no method_registry
    engine.add_contract(contract)
    engine.run()

    # Parent is suspended
    assert contract.id in engine._suspended

    # Child contract created
    child_contracts = [c for c in store.get_all_contracts() if c.parent_id == contract.id]
    assert len(child_contracts) == 1
    assert child_contracts[0].name == "root.plan"


def test_engine_dispatches_tool_method():
    """Registry dispatch works for tool actions too."""
    registry = MethodRegistry()
    handler = _RecordingHandler("tool")
    registry.register("tool", handler)

    store = MemoryStore()
    trace_store = TraceStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search"), lambda args: "result")

    worker = SequenceWorker([
        '/tool {"name": "search", "args": {"q": "test"}}',
        "",
    ])

    contract = Contract(
        id=hash_contract("root", "", [], ["report"], "", 5, ["search"], [], "human"),
        name="root",
        inputs=[],
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["search"],
    )

    engine = Engine(store, worker, trace_store, tools=tools, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    # Handler was consulted for tool type
    assert len(handler.can_handle_calls) >= 1
    # handle_method called
    assert len(handler.handle_calls) >= 1
    _, called_contract, called_action_type, _ = handler.handle_calls[0]
    assert called_contract.id == contract.id
    assert called_action_type == "tool"

    # Child contract was created (default scheduling ran)
    child_contracts = [c for c in store.get_all_contracts() if c.parent_id == contract.id]
    assert len(child_contracts) == 1

    # Tool executed via the system method path
    tool_events = trace_store.get_by_event_type("tool_executed")
    assert len(tool_events) == 1


def test_handler_swap_works():
    """Replacing a handler in the registry takes effect on the next dispatch."""
    registry = MethodRegistry()
    handler1 = _RecordingHandler("plan")
    handler2 = _RecordingHandler("plan")
    registry.register("plan", handler1)

    # First run — handler1 should be called
    store1 = MemoryStore()
    worker1 = SequenceWorker(['/plan {"reason": "first"}', ""])
    contract1 = Contract(
        id=hash_contract("c1", "", [], [], "", 5, [], [], "human"),
        name="c1", activation="", budget=5,
    )
    engine1 = Engine(store1, worker1, TraceStore(), method_registry=registry)
    engine1.add_contract(contract1)
    engine1.run()
    assert len(handler1.handle_calls) == 1
    assert len(handler2.handle_calls) == 0

    # Swap and run with new engine
    registry.register("plan", handler2)
    store2 = MemoryStore()
    worker2 = SequenceWorker(['/plan {"reason": "second"}', ""])
    contract2 = Contract(
        id=hash_contract("c2", "", [], [], "", 5, [], [], "human"),
        name="c2", activation="", budget=5,
    )
    engine2 = Engine(store2, worker2, TraceStore(), method_registry=registry)
    engine2.add_contract(contract2)
    engine2.run()

    # handler1 still has 1 call, handler2 now has 1
    assert len(handler1.handle_calls) == 1
    assert len(handler2.handle_calls) == 1


def test_multiple_handlers_isolated():
    """Each action type triggers only its own handler."""
    registry = MethodRegistry()
    plan_handler = _RecordingHandler("plan")
    tool_handler = _RecordingHandler("tool")
    registry.register("plan", plan_handler)
    registry.register("tool", tool_handler)

    # Run with a plan action
    store1 = MemoryStore()
    worker1 = SequenceWorker(['/plan {"reason": "plan only"}', ""])
    contract1 = Contract(
        id=hash_contract("c1", "", [], [], "", 5, [], [], "human"),
        name="c1", activation="", budget=5,
    )
    engine1 = Engine(store1, worker1, TraceStore(), method_registry=registry)
    engine1.add_contract(contract1)
    engine1.run()

    assert len(plan_handler.handle_calls) == 1
    assert len(tool_handler.handle_calls) == 0

    # Reset handlers for the second test
    plan_handler.handle_calls.clear()
    plan_handler.can_handle_calls.clear()

    # Run with a tool action
    store2 = MemoryStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search"), lambda args: "result")
    worker2 = SequenceWorker([
        '/tool {"name": "search", "args": {"q": "x"}}',
        "",
    ])
    contract2 = Contract(
        id=hash_contract("c2", "", [], [], "", 5, ["search"], [], "human"),
        name="c2", activation="", budget=5, tool_scope=["search"],
    )
    engine2 = Engine(store2, worker2, TraceStore(), tools=tools, method_registry=registry)
    engine2.add_contract(contract2)
    engine2.run()

    assert len(plan_handler.handle_calls) == 0
    assert len(tool_handler.handle_calls) == 1


def test_false_handler_still_falls_back():
    """When can_handle returns False, engine uses default scheduling."""
    registry = MethodRegistry()
    registry.register("plan", _FalseHandler())

    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(['/plan {"reason": "false handler"}', ""])

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

    # Even though can_handle returned False, default scheduling ran
    assert contract.id in engine._suspended
    child_contracts = [c for c in store.get_all_contracts() if c.parent_id == contract.id]
    assert len(child_contracts) == 1


def test_handler_sees_candidate_data():
    """Handler receives the full candidate object with raw output."""
    registry = MethodRegistry()
    handler = _RecordingHandler("plan")
    registry.register("plan", handler)

    store = MemoryStore()
    worker = SequenceWorker(['/plan {"reason": "check candidate"}', ""])

    contract = Contract(
        id=hash_contract("root", "", [], ["report"], "", 5, [], [], "human"),
        name="root", inputs=[], outputs=["report"], activation="", budget=5,
    )

    engine = Engine(store, worker, TraceStore(), method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    assert len(handler.handle_calls) == 1
    _, _, _, candidate = handler.handle_calls[0]
    assert candidate.raw_output == '/plan {"reason": "check candidate"}'
    assert candidate.worker_id == "sequence_worker"


def test_replan_dispatches_to_registry():
    """Registry dispatch works for replan action type."""
    registry = MethodRegistry()
    handler = _RecordingHandler("replan")
    registry.register("replan", handler)

    store = MemoryStore()
    worker = SequenceWorker(['/replan {"reason": "try again"}', ""])

    contract = Contract(
        id=hash_contract("root", "", [], ["report"], "", 5, [], [], "human"),
        name="root", inputs=[], outputs=["report"], activation="", budget=5,
    )

    engine = Engine(store, worker, TraceStore(), method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    assert len(handler.handle_calls) >= 1
    _, _, action_type, _ = handler.handle_calls[0]
    assert action_type == "replan"


def test_no_handler_triggers_no_crash():
    """Engine with registry but no matching handler still works (no crash)."""
    registry = MethodRegistry()
    # Register a tool handler but NOT a plan handler
    registry.register("tool", _RecordingHandler("tool"))

    store = MemoryStore()
    worker = SequenceWorker(['/plan {"reason": "no plan handler"}', ""])

    contract = Contract(
        id=hash_contract("root", "", [], ["report"], "", 5, [], [], "human"),
        name="root", inputs=[], outputs=["report"], activation="", budget=5,
    )

    engine = Engine(store, worker, TraceStore(), method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    # Default scheduling still happens
    assert contract.id in engine._suspended
