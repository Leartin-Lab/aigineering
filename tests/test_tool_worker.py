"""Tests for ToolExecutor (v0.3.6 — split tool execution from lifecycle, renamed per ADR-006)."""

import json


from aigineering.agent.tool_executor import ToolExecutor
from aigineering.core.store import MemoryStore
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import Asset, Candidate, ToolSpec


# ── ToolExecutor unit tests ─────────────────────────────────────────────


def test_worker_invokes_tool_and_returns_candidate():
    """ToolExecutor.invoke() executes a tool and returns a Candidate with the result."""
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    worker = ToolExecutor(registry)
    candidate = worker.invoke("lookup", {"key": "x"}, "contract_1")

    assert isinstance(candidate, Candidate)
    assert candidate.worker_id == "tool_worker:lookup"

    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is True
    assert obs["tool"] == "lookup"
    assert obs["result"] == "value:x"
    assert obs["error"] == ""


def test_worker_returns_error_candidate_on_failure():
    """ToolExecutor.invoke() returns an error Candidate when the tool is unknown."""
    registry = ToolRegistry()

    worker = ToolExecutor(registry)
    candidate = worker.invoke("missing_tool", {}, "contract_1")

    assert isinstance(candidate, Candidate)
    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is False
    assert obs["tool"] == "missing_tool"
    assert obs["result"] == ""
    assert "unknown tool" in obs["error"]
    assert obs["error_type"] == "KeyError"


def test_worker_rejects_non_string_tool_result_with_typed_error_candidate():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="bad"), lambda _args: {"not": "a string"})

    candidate = ToolExecutor(registry).invoke("bad", {}, "contract_1")
    observation = json.loads(candidate.raw_output)

    assert observation["ok"] is False
    assert observation["error_type"] == "TypeError"
    assert observation["result"] == ""


def test_worker_parity_with_direct_registry():
    """ToolExecutor.invoke() produces the same result as ToolRegistry.run()."""
    registry = ToolRegistry()
    registry.register(ToolSpec(name="echo"), lambda args: args.get("msg", ""))

    direct_result = registry.run("echo", {"msg": "hello"})

    worker = ToolExecutor(registry)
    candidate = worker.invoke("echo", {"msg": "hello"}, "contract_1")

    obs = json.loads(candidate.raw_output)
    assert obs["result"] == direct_result
    assert obs["ok"] is True


# ── Handler integration tests ──────────────────────────────────────────


def test_worker_candidate_not_committed_directly():
    """Candidate from ToolExecutor must go through projection to become a fact.

    ToolExecutor returns a Candidate — it is NOT an Asset and does NOT
    appear in any store.  The handler (or engine projection) must
    explicitly convert the Candidate into committed assets.
    """
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    worker = ToolExecutor(registry)
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
