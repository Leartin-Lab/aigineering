"""Tests for ToolMethodHandler (v0.3.5 tool-lifecycle extraction)."""

import json

from aigineering.core.engine import Engine
from aigineering.core.capability_descriptors import (
    create_mcp_descriptor,
    create_tool_descriptor,
)
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_handlers.tool import ToolMethodHandler
from aigineering.core.method_runtime import MethodRuntime
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


def test_handler_failed_tool_closes_parent_without_continuation():
    """A tool failure is terminal and cannot recursively create continuation work."""
    registry = MethodRegistry()
    handler = ToolMethodHandler()
    registry.register("tool", handler)

    store = MemoryStore()
    store._add_system_asset(
        create_tool_descriptor(
            "lookup",
            "Lookup test values.",
            {"type": "object"},
            trust_tier="configured",
        )
    )
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
    system_children = [c for c in child_contracts if c.origin == "system"]
    continuations = [c for c in child_contracts if c.origin == "continuation"]
    assert len(system_children) == 1
    assert system_children[0].name == "root.tool"
    assert continuations == []
    failed = trace_store.get_by_event_type("failed")
    assert [entry.contract_id for entry in failed] == [contract.id]


def test_handler_executes_tool_on_completion():
    """ToolMethodHandler.handle_completion runs the tool and creates assets."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")
    store._add_system_asset(
        create_tool_descriptor(
            "lookup",
            "Lookup test values.",
            {"type": "object"},
            trust_tier="configured",
        )
    )

    runtime = MethodRuntime(store, trace_store, {}, tools=tools)
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
        minting_authority=("_tool_obs_tool_child_1", "_tool_call_tool_child_1"),
    )

    result = handler.handle_completion(runtime, tool_contract, [])
    assert result is True

    obs_assets = store.get_assets_by_name("_tool_obs_tool_child_1")
    assert len(obs_assets) == 1
    assert "value:x" in obs_assets[0].content

    call_assets = [
        a for a in store.get_all_assets() if a.name.startswith("_tool_call_")
    ]
    assert len(call_assets) == 1


def test_handler_executes_mcp_tool_on_completion():
    """MCP tool actions route through MCPExecutor and create MCP system assets."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()
    store._add_system_asset(
        create_mcp_descriptor(
            "search",
            source_uri="mcp://search",
            trust_tier="configured",
        )
    )

    def search_server(tool_name, args):
        assert tool_name == "search.query"
        return f"result:{args['q']}"

    runtime = MethodRuntime(
        store,
        trace_store,
        {},
        mcp_servers={"search": search_server},
    )
    tool_contract = Contract(
        id="mcp_child_1",
        parent_id="parent_1",
        name="parent.tool",
        description=json.dumps(
            {
                "method": "tool",
                "parent_contract_id": "parent_1",
                "parent_contract_name": "parent",
                "payload": {"name": "mcp:search.query", "args": {"q": "x"}},
            },
            sort_keys=True,
        ),
        inputs=[],
        outputs=["_mcp_obs_mcp_child_1"],
        activation="_method_ctx_parent_1",
        budget=1,
        tool_scope=["mcp:search.query"],
        origin="system",
        minting_authority=("_mcp_obs_mcp_child_1", "_mcp_call_mcp_child_1"),
    )

    result = handler.handle_completion(runtime, tool_contract, [])
    assert result is True

    obs_assets = store.get_assets_by_name("_mcp_obs_mcp_child_1")
    assert len(obs_assets) == 1
    obs = json.loads(obs_assets[0].content)
    assert obs["ok"] is True
    assert obs["result"] == "result:x"

    call_assets = store.get_assets_by_name("_mcp_call_mcp_child_1")
    assert len(call_assets) == 1


def test_handler_requires_verified_tool_descriptor():
    """handle_completion blocks execution without a valid capability descriptor."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()
    calls: list[dict] = []
    tools = ToolRegistry()

    def should_not_run(args):
        calls.append(dict(args))
        return "executed"

    tools.register(ToolSpec(name="lookup"), should_not_run)
    runtime = MethodRuntime(store, trace_store, {}, tools=tools)
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
        minting_authority=("_tool_obs_tool_child_1", "_tool_call_tool_child_1"),
    )

    result = handler.handle_completion(runtime, tool_contract, [])
    assert result is True
    assert calls == []
    missing_obs = store.get_assets_by_name("_tool_obs_tool_child_1")
    assert len(missing_obs) == 1
    missing_content = json.loads(missing_obs[0].content)
    assert missing_content["ok"] is False
    assert "descriptor is missing" in missing_content["error"]

    store = MemoryStore()
    trace_store = TraceStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), should_not_run)
    store._add_system_asset(
        create_tool_descriptor(
            "lookup",
            "Lookup test values.",
            {"type": "object"},
            trust_tier="untrusted",
        )
    )
    runtime = MethodRuntime(store, trace_store, {}, tools=tools)
    result = handler.handle_completion(runtime, tool_contract, [])
    assert result is True
    assert calls == []
    invalid_obs = store.get_assets_by_name("_tool_obs_tool_child_1")
    assert len(invalid_obs) == 1
    invalid_content = json.loads(invalid_obs[0].content)
    assert invalid_content["ok"] is False
    assert "descriptor failed verification" in invalid_content["error"]


def test_handler_rejects_unknown_tool():
    """handle_completion records error when no ToolRegistry is configured."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()

    runtime = MethodRuntime(store, trace_store, {}, tools=None)
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
        minting_authority=("_tool_obs_tool_child_1", "_tool_call_tool_child_1"),
    )

    result = handler.handle_completion(runtime, tool_contract, [])
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
    store._add_system_asset(
        create_tool_descriptor(
            "lookup",
            "Lookup test values.",
            {"type": "object"},
            trust_tier="configured",
        )
    )

    runtime = MethodRuntime(store, trace_store, {}, tools=tools)
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
        minting_authority=("_tool_obs_tool_child_1", "_tool_call_tool_child_1"),
    )

    result = handler.handle_completion(runtime, tool_contract, [])
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
            {
                "method": "plan",
                "parent_contract_id": "parent_1",
                "parent_contract_name": "parent",
                "payload": {},
            },
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
    store = MemoryStore()
    store._add_system_asset(
        create_tool_descriptor(
            "lookup",
            "Lookup test values.",
            {"type": "object"},
            trust_tier="configured",
        )
    )

    worker = SequenceWorker(
        [
            '/tool {"name": "lookup", "args": {"key": "x"}}',
            '/exec {"outputs": {"report": "final after tool"}}',
        ]
    )
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
    assert obs_assets[0].origin == "tool"
    assert obs_assets[0].trust_tier == "observed"
    assert "value:x" in obs_assets[0].content

    call_assets = [
        a for a in store.get_all_assets() if a.name.startswith("_tool_call_")
    ]
    assert len(call_assets) == 1
    assert call_assets[0].promptable is False

    tool_events = trace_store.get_by_event_type("tool_executed")
    assert len(tool_events) == 1
    assert tool_events[0].relation_target == "lookup"
    assert tool_events[0].authority_result == "accepted"

    continued = trace_store.get_by_event_type("method_continuation_scheduled")
    assert len(continued) == 1
    assert continued[0].relation_type == "tool"


def test_tool_without_handler_fails_closed():
    """Engine must not execute tools without a registered ToolMethodHandler."""
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    worker = SequenceWorker(
        [
            '/tool {"name": "lookup", "args": {"key": "x"}}',
            '/exec {"outputs": {"report": "final"}}',
        ]
    )
    store = MemoryStore()
    store._add_system_asset(
        create_tool_descriptor(
            "lookup",
            "Lookup test values.",
            {"type": "object"},
            trust_tier="configured",
        )
    )
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

    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert obs_assets == []

    tool_events = trace_store.get_by_event_type("tool_executed")
    assert tool_events == []
    missing = trace_store.get_by_event_type("method_handler_missing")
    assert len(missing) == 1
    assert missing[0].authority_result == "rejected"
    assert missing[0].relation_type == "tool"


def test_tool_without_handler_does_not_bypass_descriptor_gate():
    """Missing ToolMethodHandler must fail before any tool execution."""
    calls: list[dict] = []
    tools = ToolRegistry()

    def should_not_run(args):
        calls.append(dict(args))
        return "executed"

    tools.register(ToolSpec(name="lookup"), should_not_run)
    worker = SequenceWorker(['/tool {"name": "lookup", "args": {"key": "x"}}'])
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

    assert calls == []
    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert obs_assets == []
    tool_events = trace_store.get_by_event_type("tool_executed")
    assert tool_events == []
    missing = trace_store.get_by_event_type("method_handler_missing")
    assert len(missing) == 1
    assert missing[0].authority_result == "rejected"


def test_handler_satisfies_protocol():
    """ToolMethodHandler is structurally compatible with MethodHandler."""
    handler = ToolMethodHandler()
    assert hasattr(handler, "can_handle")
    assert hasattr(handler, "handle_method")
    assert hasattr(handler, "handle_completion")
    assert callable(handler.can_handle)
    assert callable(handler.handle_method)
    assert callable(handler.handle_completion)
