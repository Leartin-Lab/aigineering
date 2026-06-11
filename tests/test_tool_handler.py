"""Tests for ToolMethodHandler (v0.3.5 tool-lifecycle extraction)."""

import json

from aigineering.core.engine import Engine
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_handlers.tool import ToolMethodHandler
from aigineering.core.methods import method_payload
from aigineering.core.store import MemoryStore
from aigineering.core.tools import ToolRegistry
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Contract, ToolSpec


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

def test_handler_can_handle_tool():
    handler = ToolMethodHandler()
    assert handler.can_handle("tool") is True
    assert handler.can_handle("plan") is False
    assert handler.can_handle("replan") is False


def test_handler_schedules_tool_child():
    """ToolMethodHandler.handle_method creates a child contract via scheduler."""
    registry = MethodRegistry()
    handler = ToolMethodHandler()
    registry.register("tool", handler)

    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(['/tool {"name": "lookup", "args": {"key": "x"}}', ""])

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

    child_contracts = [
        c for c in store.get_all_contracts() if c.parent_id == contract.id
    ]
    assert len(child_contracts) == 1
    assert child_contracts[0].name == "root.tool"
    assert child_contracts[0].origin == "system"


def test_handler_executes_tool_on_completion():
    """ToolMethodHandler.handle_completion runs the tool and creates assets."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    class MinimalEngine:
        _store = store
        _trace = trace_store
        _tools = tools
        _budget: dict[str, int] = {}

        def _add_trace(self, contract_id, event_type, **kwargs):
            pass

        def _resolve_budget(self, contract):
            return 1

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
                "payload": {"name": "lookup", "args": {"key": "x"}},
            },
            sort_keys=True,
        ),
        inputs=[],
        outputs=["_tool_obs_tool_child_1"],
        activation="_method_ctx_parent_1",
        budget=1,
        tool_scope=["lookup"],
        origin="system",
    )

    result = handler.handle_completion(engine, tool_contract, [])
    assert result is True

    obs_assets = store.get_assets_by_name("_tool_obs_tool_child_1")
    assert len(obs_assets) == 1
    assert "value:x" in obs_assets[0].content

    call_assets = [
        a for a in store.get_all_assets()
        if a.name.startswith("_tool_call_")
    ]
    assert len(call_assets) == 1


def test_handler_rejects_unknown_tool():
    """handle_completion records error when no ToolRegistry is configured."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()

    class MinimalEngine:
        _store = store
        _trace = trace_store
        _tools = None
        _budget: dict[str, int] = {}

        def _add_trace(self, contract_id, event_type, **kwargs):
            pass

        def _resolve_budget(self, contract):
            return 1

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
                "payload": {"name": "unknown_tool", "args": {}},
            },
            sort_keys=True,
        ),
        inputs=[],
        outputs=["_tool_obs_tool_child_1"],
        activation="_method_ctx_parent_1",
        budget=1,
        tool_scope=["unknown_tool"],
        origin="system",
    )

    result = handler.handle_completion(engine, tool_contract, [])
    assert result is True

    obs_assets = store.get_assets_by_name("_tool_obs_tool_child_1")
    assert len(obs_assets) == 1
    content = json.loads(obs_assets[0].content)
    assert content["ok"] is False
    assert "no ToolRegistry" in content["error"]


def test_handler_respects_tool_scope():
    """handle_completion rejects tool not in contract.tool_scope."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: "value")

    class MinimalEngine:
        _store = store
        _trace = trace_store
        _tools = tools
        _budget: dict[str, int] = {}

        def _add_trace(self, contract_id, event_type, **kwargs):
            pass

        def _resolve_budget(self, contract):
            return 1

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
                "payload": {"name": "lookup", "args": {"key": "x"}},
            },
            sort_keys=True,
        ),
        inputs=[],
        outputs=["_tool_obs_tool_child_1"],
        activation="_method_ctx_parent_1",
        budget=1,
        tool_scope=[],
        origin="system",
    )

    result = handler.handle_completion(engine, tool_contract, [])
    assert result is True

    obs_assets = store.get_assets_by_name("_tool_obs_tool_child_1")
    assert len(obs_assets) == 1
    content = json.loads(obs_assets[0].content)
    assert content["ok"] is False
    assert "not in contract.tool_scope" in content["error"]


def test_handler_returns_false_for_non_tool():
    """handle_completion returns False for non-tool method contracts."""
    handler = ToolMethodHandler()

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

    engine = MinimalEngine()
    plan_contract = Contract(
        id="plan_child_1",
        parent_id="parent_1",
        name="parent.plan",
        description=json.dumps(
            {"method": "plan", "parent_contract_id": "parent_1",
             "parent_contract_name": "parent", "payload": {}},
            sort_keys=True,
        ),
        inputs=[],
        outputs=["_plan_result_plan_child_1"],
        activation="_method_ctx_parent_1",
        budget=1,
        origin="system",
    )

    result = handler.handle_completion(engine, plan_contract, [])
    assert result is False


# ── Engine integration tests ──────────────────────────────────────────

def test_engine_uses_tool_handler():
    """Full engine flow: registered ToolMethodHandler drives tool execution."""
    registry = MethodRegistry()
    handler = ToolMethodHandler()
    registry.register("tool", handler)

    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    worker = SequenceWorker([
        '/tool {"name": "lookup", "args": {"key": "x"}}',
        '/exec {"outputs": {"report": "final after tool"}}',
    ])
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
    engine = Engine(store, worker, trace_store, tools=tools, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    reports = store.get_assets_by_name("report")
    assert len(reports) == 1
    assert reports[0].content == "final after tool"

    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert len(obs_assets) == 1
    assert obs_assets[0].origin == "system"
    assert "value:x" in obs_assets[0].content

    call_assets = [
        a for a in store.get_all_assets()
        if a.name.startswith("_tool_call_")
    ]
    assert len(call_assets) == 1
    assert call_assets[0].promptable is False

    tool_events = trace_store.get_by_event_type("tool_executed")
    assert len(tool_events) == 1
    assert tool_events[0].relation_target == "lookup"
    assert tool_events[0].authority_result == "accepted"

    resumed = trace_store.get_by_event_type("method_resumed")
    assert len(resumed) == 1
    assert resumed[0].relation_type == "tool"


def test_fallback_without_handler():
    """Engine works without ToolMethodHandler (backward compat)."""
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    worker = SequenceWorker([
        '/tool {"name": "lookup", "args": {"key": "x"}}',
        '/exec {"outputs": {"report": "final"}}',
    ])
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
    engine = Engine(store, worker, trace_store, tools=tools)
    engine.add_contract(contract)
    engine.run()

    reports = store.get_assets_by_name("report")
    assert len(reports) == 1

    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert len(obs_assets) == 1
    assert "value:x" in obs_assets[0].content

    tool_events = trace_store.get_by_event_type("tool_executed")
    assert len(tool_events) == 1
    assert tool_events[0].authority_result == "accepted"


def test_handler_satisfies_protocol():
    """ToolMethodHandler is structurally compatible with MethodHandler."""
    handler = ToolMethodHandler()
    assert hasattr(handler, "can_handle")
    assert hasattr(handler, "handle_method")
    assert hasattr(handler, "handle_completion")
    assert callable(handler.can_handle)
    assert callable(handler.handle_method)
    assert callable(handler.handle_completion)
