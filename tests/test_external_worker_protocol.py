"""Tool and MCP effects cross the same claimed Candidate boundary as LLMs."""

from __future__ import annotations

import json
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mcp_worker import MCPWorker
from aigineering.agent.tool_worker import ToolWorker
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.core.capability_descriptors import (
    create_mcp_descriptor,
    create_tool_descriptor,
)
from aigineering.core.methods import method_contract, system_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.actions import parse_action
from aigineering.protocol.types import Contract, ToolSpec


def _install_method(store, parent: Contract, action_text: str, descriptor):
    ingress = candidate_runtime(store)
    parent = ingress.accept_contract(parent)
    ingress.accept_asset(descriptor, source="capability", allow_protected=True)
    child = method_contract(parent, parse_action(action_text))
    child = ingress.accept_contract(child)
    ingress.accept_asset(
        system_asset(
            name=f"_method_ctx_{parent.id}",
            content=action_text,
            created_by=parent.id,
        ),
        source="method",
        allow_protected=True,
    )
    return child, ingress


def test_tool_worker_effect_is_observed_candidate_fact():
    store = SQLiteStore(":memory:")
    parent = Contract(
        id="task:tool-parent",
        name="tool-parent",
        outputs=("report",),
        tool_scope=("lookup",),
        budget=2,
    )
    child, runtime = _install_method(
        store,
        parent,
        '/tool {"name": "lookup", "args": {"key": "x"}}',
        create_tool_descriptor(
            "lookup", "Lookup values", {"type": "object"}, trust_tier="configured"
        ),
    )
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")
    assert (
        claim_next_package(store, worker_id="llm:untrusted", contract_id=child.id)
        is None
    )
    worker = ToolWorker(registry, worker_id="tool_worker:local")
    assert worker.registration().capabilities == ("tool-execution",)
    host = hosted_worker(
        store,
        worker,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    claimed = claim_next_package(store, worker_id=host.worker_id, contract_id=child.id)
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert not result["rejected"], result["rejected"]
    assert result["status"] == "accepted", result
    observation = store.get_assets_by_name(child.outputs[0])[0]
    assert observation.origin == "worker"
    assert observation.trust_tier == "observed"
    assert json.loads(observation.content)["result"] == "value:x"
    store.close()


def test_mcp_worker_effect_is_observed_candidate_fact():
    store = SQLiteStore(":memory:")
    parent = Contract(
        id="task:mcp-parent",
        name="mcp-parent",
        outputs=("report",),
        tool_scope=("mcp:search.query",),
        budget=2,
    )
    child, runtime = _install_method(
        store,
        parent,
        '/tool {"name": "mcp:search.query", "args": {"q": "facts"}}',
        create_mcp_descriptor(
            "search",
            source_uri="mcp://search",
            trust_tier="verified",
            tool_name="search.query",
        ),
    )
    worker = MCPWorker(
        {"search": lambda tool, args: f"{tool}:{args['q']}"},
        worker_id="mcp_worker:search",
    )
    assert (
        claim_next_package(store, worker_id="llm:untrusted", contract_id=child.id)
        is None
    )
    assert worker.registration().capabilities == ("mcp-execution",)
    host = hosted_worker(
        store,
        worker,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    claimed = claim_next_package(store, worker_id=host.worker_id, contract_id=child.id)
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "accepted", result
    observation = store.get_assets_by_name(child.outputs[0])[0]
    assert observation.origin == "worker"
    assert observation.trust_tier == "observed"
    assert json.loads(observation.content)["result"] == "search.query:facts"
    store.close()
