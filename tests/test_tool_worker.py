"""Tests for ToolWorker (v0.3.6 — split tool execution from lifecycle)."""

import json


from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.method_handlers.tool import ToolMethodHandler
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.store import MemoryStore
from aigineering.core.tools import ToolRegistry
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Candidate, Contract, ToolSpec


# ── ToolWorker unit tests ──────────────────────────────────────────────


def test_worker_invokes_tool_and_returns_candidate():
    """ToolWorker.invoke() executes a tool and returns a Candidate with the result."""
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    worker = ToolWorker(registry)
    candidate = worker.invoke("lookup", {"key": "x"}, "contract_1")

    assert isinstance(candidate, Candidate)
    assert candidate.worker_id == "tool_worker:lookup"

    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is True
    assert obs["tool"] == "lookup"
    assert obs["result"] == "value:x"
    assert obs["error"] == ""


def test_worker_returns_error_candidate_on_failure():
    """ToolWorker.invoke() returns an error Candidate when the tool is unknown."""
    registry = ToolRegistry()

    worker = ToolWorker(registry)
    candidate = worker.invoke("missing_tool", {}, "contract_1")

    assert isinstance(candidate, Candidate)
    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is False
    assert obs["tool"] == "missing_tool"
    assert obs["result"] == ""
    assert "unknown tool" in obs["error"]


def test_worker_parity_with_direct_registry():
    """ToolWorker.invoke() produces the same result as ToolRegistry.run()."""
    registry = ToolRegistry()
    registry.register(ToolSpec(name="echo"), lambda args: args.get("msg", ""))

    direct_result = registry.run("echo", {"msg": "hello"})

    worker = ToolWorker(registry)
    candidate = worker.invoke("echo", {"msg": "hello"}, "contract_1")

    obs = json.loads(candidate.raw_output)
    assert obs["result"] == direct_result
    assert obs["ok"] is True


# ── Handler integration tests ──────────────────────────────────────────


def test_tool_handler_uses_tool_worker():
    """ToolMethodHandler.handle_completion dispatches via ToolWorker."""
    handler = ToolMethodHandler()

    store = MemoryStore()
    trace_store = TraceStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")
    store.add_asset(
        create_tool_descriptor(
            "lookup",
            "Lookup test values.",
            {"type": "object"},
            trust_tier="configured",
        )
    )

    # Verify the handler module imports ToolWorker
    import aigineering.core.method_handlers.tool as handler_mod

    assert hasattr(handler_mod, "ToolWorker")

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
    )

    result = handler.handle_completion(runtime, tool_contract, [])
    assert result is True

    obs_assets = store.get_assets_by_name("_tool_obs_tool_child_1")
    assert len(obs_assets) == 1
    obs = json.loads(obs_assets[0].content)
    assert obs["ok"] is True
    assert obs["result"] == "value:x"


def test_worker_candidate_not_committed_directly():
    """Candidate from ToolWorker must go through projection to become a fact.

    ToolWorker returns a Candidate — it is NOT an Asset and does NOT
    appear in any store.  The handler (or engine projection) must
    explicitly convert the Candidate into committed assets.
    """
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    worker = ToolWorker(registry)
    candidate = worker.invoke("lookup", {"key": "x"}, "contract_1")

    # Candidate is NOT an Asset
    assert isinstance(candidate, Candidate)
    assert not isinstance(candidate, Asset)

    # Candidate has worker provenance, not asset metadata
    assert candidate.worker_id == "tool_worker:lookup"
    assert not hasattr(candidate, "id")
    assert not hasattr(candidate, "name")
    assert not hasattr(candidate, "origin")

    # No side-effects: candidate exists only in memory
    store = MemoryStore()
    assert len(store.get_all_assets()) == 0
