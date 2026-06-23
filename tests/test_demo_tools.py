"""Tests for demo tools — memory-backed built-in tools (v0.5.0-alpha.2)."""

from __future__ import annotations

import json

import pytest

from aigineering.agent.demo_tools import register_demo_tools, reset_demo_store
from aigineering.agent.mcp_executor import MCPExecutor
from aigineering.agent.tool_executor import ToolExecutor
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.engine import Engine
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_handlers.tool import ToolMethodHandler
from aigineering.core.store import MemoryStore
from aigineering.core.tools import ToolRegistry
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Candidate, Contract, ToolSpec


# ── SequenceWorker (same pattern as test_engine.py) ─────────────────────


class SequenceWorker:
    worker_id = "sequence_worker"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[Contract, list]] = []

    def invoke(self, contract, disclosed_assets):
        from aigineering.protocol.types import Candidate

        self.calls.append((contract, disclosed_assets))
        raw_output = self._outputs.pop(0) if self._outputs else ""
        return Candidate(worker_id=self.worker_id, raw_output=raw_output)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_demo_store():
    """Isolate demo store between tests."""
    reset_demo_store()
    yield
    reset_demo_store()


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_demo_tools(reg)
    return reg


def _add_demo_tool_descriptors(store: MemoryStore, *names: str) -> None:
    for name in names:
        store.add_asset(
            create_tool_descriptor(
                name,
                f"Demo tool: {name}",
                {"type": "object"},
                trust_tier="configured",
            )
        )


# ── Unit: demo file_read / file_write (memory-backed) ───────────────────


def test_demo_file_read_write():
    """Memory-backed file_read/file_write through ToolRegistry.

    file_write stores content at path, file_read retrieves it.
    No real filesystem access.
    """
    registry = _make_registry()

    # Write a file
    result = registry.run(
        "file_write", {"path": "/notes.txt", "content": "hello world"}
    )
    assert result == "ok"

    # Read it back
    content = registry.run("file_read", {"path": "/notes.txt"})
    assert content == "hello world"

    # Read non-existent file returns ""
    missing = registry.run("file_read", {"path": "/no/such/file"})
    assert missing == ""

    # Overwrite
    registry.run("file_write", {"path": "/notes.txt", "content": "updated"})
    assert registry.run("file_read", {"path": "/notes.txt"}) == "updated"


def test_demo_file_read_write_via_tool_worker():
    """ToolExecutor.invoke() wraps file_read/file_write results as Candidates."""
    registry = _make_registry()
    worker = ToolExecutor(registry)

    # write via ToolExecutor
    wc = worker.invoke("file_write", {"path": "/a.txt", "content": "data"}, "c1")
    assert isinstance(wc, Candidate)
    w_obs = json.loads(wc.raw_output)
    assert w_obs["ok"] is True
    assert w_obs["tool"] == "file_write"
    assert w_obs["result"] == "ok"

    # read via ToolExecutor
    rc = worker.invoke("file_read", {"path": "/a.txt"}, "c1")
    r_obs = json.loads(rc.raw_output)
    assert r_obs["ok"] is True
    assert r_obs["tool"] == "file_read"
    assert r_obs["result"] == "data"


# ── Unit: demo search tool ──────────────────────────────────────────────


def test_demo_search_tool():
    """Demo search tool returns results for query string."""
    registry = _make_registry()

    result = registry.run("search", {"q": "thermal efficiency"})
    assert "Found results for: thermal efficiency" in result

    # No query → empty q
    assert registry.run("search", {}) == "Found results for: "


def test_demo_search_tool_via_tool_worker():
    """ToolExecutor wraps search results as a Candidate."""
    registry = _make_registry()
    worker = ToolExecutor(registry)

    candidate = worker.invoke("search", {"q": "hello"}, "c1")
    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is True
    assert obs["tool"] == "search"
    assert "hello" in obs["result"]


# ── Unit: demo search via MCPExecutor pattern ────────────────────────────


def test_demo_search_via_mcp_worker():
    """Demo search tool exercised through MCPExecutor pattern.

    MCPExecutor uses server-prefixed tool names (e.g. ``search.query``)
    and dispatches to server callables.  This tests the MCP pipeline
    with a mock search server.
    """

    # Mock MCP search server
    def search_server(tool_name: str, args: dict) -> str:
        return json.dumps({"results": [f"hit: {args.get('q', '')}"]})

    worker = MCPExecutor(mcp_servers={"search": search_server})
    candidate = worker.invoke("search.query", {"q": "thermal"}, "contract_1")

    assert isinstance(candidate, Candidate)
    assert candidate.worker_id == "mcp_worker:search.query"

    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is True
    assert obs["tool"] == "search.query"
    assert "thermal" in obs["result"]
    assert "hit:" in obs["result"]


# ── Integration: full pipeline plan→tool→resume→complete ────────────────


def test_demo_workflow_full_pipeline():
    """Full pipeline: plan→tool→resume→complete with demo tools.

    Exercises the ToolExecutor pipeline end-to-end through the engine:
    1. Parent contract with tool_scope triggers /tool action
    2. ToolMethodHandler + Engine execute tool via ToolExecutor
    3. Observation asset is committed, parent resumes
    4. Parent completes with final output
    5. Trace shows tool_executed events
    """
    registry = MethodRegistry()
    handler = ToolMethodHandler()
    registry.register("tool", handler)

    tools = ToolRegistry()
    register_demo_tools(tools)

    worker = SequenceWorker(
        [
            '/tool {"name": "file_write", "args": {"path": "/log", "content": "step 1 done"}}',
            '/tool {"name": "file_read", "args": {"path": "/log"}}',
            '/exec {"outputs": {"report": "all steps complete"}}',
        ]
    )

    store = MemoryStore()
    _add_demo_tool_descriptors(store, "file_write", "file_read", "search")
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["file_write", "file_read", "search"],
    )

    engine = Engine(
        store,
        worker,
        trace_store,
        tools=tools,
        method_registry=registry,
    )
    engine.add_contract(contract)
    engine.run()

    # ── Verify final output ──────────────────────────────────────────────
    reports = store.get_assets_by_name("report")
    assert len(reports) == 1
    assert reports[0].content == "all steps complete"

    # Both tool calls should produce call and obs assets
    call_assets = [
        a for a in store.get_all_assets() if a.name.startswith("_tool_call_")
    ]
    assert len(call_assets) == 2

    all_obs = [a for a in store.get_all_assets() if a.name.startswith("_tool_obs_")]
    assert len(all_obs) == 2

    # ── Verify trace contains tool_executed events ───────────────────────
    tool_events = trace_store.get_by_event_type("tool_executed")
    assert len(tool_events) >= 1
    assert tool_events[0].authority_result == "accepted"

    # ── Verify trace shows complete pipeline phases ──────────────────────
    event_types = {e.event_type for e in trace_store.get_all()}
    assert "activation" in event_types
    assert "disclosure" in event_types
    assert "method_scheduled" in event_types
    assert "tool_executed" in event_types
    assert "method_continuation_scheduled" in event_types
    assert "complete" in event_types


def test_demo_workflow_search_tool():
    """Full pipeline with demo search tool: /tool search → continuation → /exec.

    Contract with tool_scope=["search"] triggers search tool,
    observation is made available to a continuation contract, which
    produces final output.
    """
    registry = MethodRegistry()
    handler = ToolMethodHandler()
    registry.register("tool", handler)

    tools = ToolRegistry()
    register_demo_tools(tools)

    worker = SequenceWorker(
        [
            '/tool {"name": "search", "args": {"q": "efficiency"}}',
            '/exec {"outputs": {"report": "used search tool"}}',
        ]
    )

    store = MemoryStore()
    _add_demo_tool_descriptors(store, "search")
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["search"],
    )

    engine = Engine(store, worker, trace_store, tools=tools, method_registry=registry)
    engine.add_contract(contract)
    engine.run()

    # Final output committed
    assert len(store.get_assets_by_name("report")) == 1

    # Search observation exists
    obs = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert len(obs) == 1
    obs_content = json.loads(obs[0].content)
    assert obs_content["tool"] == "search"
    assert "Found results for: efficiency" in obs_content["result"]

    # Trace contains tool_executed
    tool_events = trace_store.get_by_event_type("tool_executed")
    assert len(tool_events) == 1
    assert tool_events[0].relation_target == "search"


def test_demo_tool_registry_specs_exposed():
    """Demo tool specs are exposed via list_specs, handlers are private."""
    registry = _make_registry()

    all_specs = registry.list_specs()
    names = {s.name for s in all_specs}
    assert names == {"file_read", "file_write", "search"}

    # Verify specs are ToolSpec objects without handler access
    for spec in all_specs:
        assert isinstance(spec, ToolSpec)
        assert not hasattr(spec, "handler")

    # Scope filtering works
    scoped = registry.list_specs(scope=["search"])
    assert len(scoped) == 1
    assert scoped[0].name == "search"

    # Unknown tool raises KeyError
    with pytest.raises(KeyError, match="unknown tool"):
        registry.run("missing_tool", {})


def test_demo_tools_via_tool_registry_direct():
    """All demo tools are reachable via ToolRegistry.run()."""
    registry = _make_registry()

    # file_write + file_read
    assert registry.run("file_write", {"path": "/f", "content": "hello"}) == "ok"
    assert registry.run("file_read", {"path": "/f"}) == "hello"

    # search
    result = registry.run("search", {"q": "test"})
    assert "Found results for: test" == result

    # Missing path in file_read raises KeyError (lambda accesses args["path"])
    with pytest.raises(KeyError):
        registry.run("file_read", {})


def test_demo_store_isolation():
    """reset_demo_store clears the memory filesystem between tests."""
    registry = _make_registry()

    registry.run("file_write", {"path": "/x", "content": "data"})
    assert registry.run("file_read", {"path": "/x"}) == "data"

    reset_demo_store()
    assert registry.run("file_read", {"path": "/x"}) == ""
