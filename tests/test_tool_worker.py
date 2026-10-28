"""Tests for ToolExecutor (v0.3.6 — split tool execution from lifecycle, renamed per ADR-006)."""

import json
from dataclasses import replace
import pytest

from aigineering.agent.tool_executor import ToolExecutor
from aigineering.agent.worker import WorkerExecutionError
from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.store import MemoryStore
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import Asset, Candidate, Contract, ToolSpec


def _tool_contract(name: str = "lookup") -> Contract:
    return Contract(
        id="contract_1",
        description=json.dumps({"payload": {"name": name, "args": {}}}),
        outputs=("_tool_obs_contract_1",),
        tool_scope=(name,),
    )


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
    assert candidate.metadata["contract_id"] == "contract_1"
    assert candidate.metadata["tool"] == "lookup"
    assert candidate.metadata["tool_version"] == "0.1.0"
    assert candidate.metadata["result_bytes"] == len(b"value:x")
    assert isinstance(candidate.metadata["duration_ms"], int)
    assert candidate.metadata["error_type"] == ""
    assert candidate.metadata["retryable"] is False


def test_worker_raises_safe_error_when_tool_is_unknown():
    registry = ToolRegistry()

    worker = ToolExecutor(registry)
    with pytest.raises(WorkerExecutionError) as error:
        worker.invoke("missing_tool", {}, "contract_1")
    assert error.value.code == "tool_unavailable"
    assert str(error.value) == "Tool is unavailable."


def test_worker_rejects_non_string_tool_result_without_candidate():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="bad"), lambda _args: {"not": "a string"})

    with pytest.raises(WorkerExecutionError) as error:
        ToolExecutor(registry).invoke("bad", {}, "contract_1")
    assert error.value.code == "tool_execution_failed"
    assert error.value.__cause__ is not None


def test_handler_type_error_is_safe_generic_execution_failure():
    registry = ToolRegistry()

    def fail(_args):
        raise TypeError("private handler detail")

    registry.register(ToolSpec(name="bad"), fail)
    with pytest.raises(WorkerExecutionError) as error:
        ToolExecutor(registry).invoke("bad", {}, "contract_1")
    assert error.value.code == "tool_execution_failed"
    assert str(error.value) == "Tool execution failed."
    assert "private handler detail" not in str(error.value)


def test_worker_validates_tool_output_before_success_observation():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="lookup",
            version="7",
            output_schema={"type": "object", "required": ["value"]},
        ),
        lambda _args: '{"other":"x"}',
    )

    with pytest.raises(WorkerExecutionError) as error:
        ToolExecutor(registry).invoke("lookup", {}, "contract_1")
    assert error.value.code == "tool_schema_invalid"
    assert isinstance(error.value.__cause__, ValueError)


def test_negative_business_json_remains_a_successful_observation():
    registry = ToolRegistry()
    result = '{"ok":false,"reason":"business rejection"}'
    registry.register(ToolSpec(name="check"), lambda _args: result)

    candidate = ToolExecutor(registry).invoke("check", {}, "contract_1")
    observation = json.loads(candidate.raw_output)

    assert observation["ok"] is True
    assert observation["result"] == result
    assert observation["error"] == ""


def test_tool_worker_rejects_descriptor_contract_drift_before_handler():
    called = False

    def handler(_args):
        nonlocal called
        called = True
        return "should not run"

    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup", version="2"), handler)
    descriptor = create_tool_descriptor(
        "lookup", "lookup", {}, version="1", source_uri="python:lookup"
    )
    contract = Contract(
        id="contract_1",
        description=json.dumps({"payload": {"name": "lookup", "args": {}}}),
        outputs=("_tool_obs_contract_1",),
        tool_scope=("lookup",),
    )

    with pytest.raises(WorkerExecutionError) as error:
        ToolWorker(registry).invoke(contract, [descriptor])
    assert error.value.code == "tool_descriptor_mismatch"
    assert called is False


def test_tool_worker_selects_unique_matching_descriptor_independent_of_order():
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="lookup", version="2"), lambda _args: calls.append(1) or "ok"
    )
    old = create_tool_descriptor("lookup", "old", {}, version="1")
    current = create_tool_descriptor("lookup", "current", {}, version="2")
    worker = ToolWorker(registry)

    for disclosed in ([old, current], [current, old]):
        candidate = worker.invoke(_tool_contract(), disclosed)
        wrapped = json.loads(candidate.raw_output)
        observation = json.loads(wrapped["outputs"]["_tool_obs_contract_1"])
        assert observation["ok"] is True

    assert len(calls) == 2


def test_tool_worker_rejects_older_descriptor_when_it_is_the_only_disclosed_match():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup", version="2"), lambda _args: "ok")
    old = create_tool_descriptor("lookup", "old", {}, version="1")

    with pytest.raises(WorkerExecutionError) as error:
        ToolWorker(registry).invoke(_tool_contract(), [old])

    assert error.value.code == "tool_descriptor_mismatch"


def test_tool_worker_rejects_multiple_distinct_matching_descriptors():
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="lookup", version="2"), lambda _args: calls.append(1) or "ok"
    )
    first = create_tool_descriptor("lookup", "first description", {}, version="2")
    second = create_tool_descriptor("lookup", "second description", {}, version="2")

    assert first.id != second.id
    with pytest.raises(WorkerExecutionError) as error:
        ToolWorker(registry).invoke(_tool_contract(), [first, second])

    assert error.value.code == "tool_descriptor_ambiguous"
    assert (
        str(error.value)
        == "More than one disclosed tool descriptor matches the registered tool contract."
    )
    assert calls == []


def test_tool_worker_treats_repeated_descriptor_id_as_one_match():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup", version="2"), lambda _args: "ok")
    descriptor = create_tool_descriptor("lookup", "current", {}, version="2")

    candidate = ToolWorker(registry).invoke(_tool_contract(), [descriptor, descriptor])

    wrapped = json.loads(candidate.raw_output)
    observation = json.loads(wrapped["outputs"]["_tool_obs_contract_1"])
    assert observation["ok"] is True


@pytest.mark.parametrize("disclosed_kind", ["missing", "tampered"])
def test_tool_worker_keeps_invalid_error_when_no_valid_named_descriptor(disclosed_kind):
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda _args: "ok")
    assets = []
    if disclosed_kind == "tampered":
        valid = create_tool_descriptor("lookup", "lookup", {})
        assets = [replace(valid, content='{"tampered":true}')]

    with pytest.raises(WorkerExecutionError) as error:
        ToolWorker(registry).invoke(_tool_contract(), assets)

    assert error.value.code == "tool_descriptor_invalid"


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


@pytest.mark.parametrize("args", ["malformed", [], None, 1, True])
def test_tool_worker_rejects_non_object_arguments_before_handler(args):
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="lookup"), lambda value: calls.append(value) or "ok"
    )
    descriptor = create_tool_descriptor(
        "lookup", "lookup", {}, source_uri="python:lookup"
    )
    contract = Contract(
        id="contract_1",
        description=json.dumps({"payload": {"name": "lookup", "args": args}}),
        outputs=("_tool_obs_contract_1",),
        tool_scope=("lookup",),
    )

    with pytest.raises(WorkerExecutionError) as error:
        ToolWorker(registry).invoke(contract, [descriptor])
    assert calls == []
    assert error.value.code == "tool_arguments_invalid"


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
