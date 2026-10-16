"""Tool and MCP effects cross the same claimed Candidate boundary as LLMs."""

from __future__ import annotations

import json
import pytest
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mcp_worker import MCPWorker
from aigineering.run_failure_diagnostics import invocation_failure_detail
from aigineering.task_diagnostics import task_diagnostics, task_runs
from aigineering.agent.tool_worker import ToolWorker
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
)
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
    assert worker.registration().capabilities == ("tool-execution", "tool:lookup")
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


def test_signed_mcp_callable_failure_closes_claim_without_observation():
    store = SQLiteStore(":memory:")
    parent = Contract(
        id="task:mcp-failure-parent",
        name="mcp-failure-parent",
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

    def failing_server(_tool: str, _args: dict) -> str:
        raise KeyError("private MCP failure detail")

    worker = MCPWorker({"search": failing_server}, worker_id="mcp_worker:search")
    host = hosted_worker(
        store,
        worker,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    request_id = "signed-mcp-call-failure-claim"
    claim_proposal = host.claim_candidate(request_id=request_id, contract_id=child.id)
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=child.id,
        claim_candidate=claim_proposal,
    )
    assert claimed is not None
    request_record = next(
        record
        for _, record in store.scan_runtime_records(
            record_type="worker.claim.requested"
        )
        if record.payload.get("idempotency_key") == request_id
    )
    receipt_record = next(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("candidate_id") == claim_proposal.id
    )
    grant_record = next(
        record
        for _, record in store.scan_runtime_records(record_type="claim.granted")
        if record.payload.get("claim_id") == claimed.package.claim_id
    )
    assert request_record.payload["candidate_id"] == claim_proposal.id
    assert request_record.causal_parents == (receipt_record.id,)
    assert grant_record.payload["contract_id"] == child.id
    assert grant_record.causal_parents == (request_record.id,)

    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(claimed, host, store)

    assert store.get_assets_by_name(child.outputs[0]) == []
    failure = next(
        record
        for _, record in store.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
        if record.payload.get("contract_id") == child.id
    )
    assert failure.payload["category"] == "worker_error:mcp_call_failed"
    assert failure.payload["diagnostic"] == "MCP call failed."
    run_view = task_runs(store, child.id)
    assert run_view["ok"] is True
    failed_run = next(
        run
        for run in run_view["data"]["runs"]
        if run["claim_id"] == claimed.package.claim_id
    )
    assert failed_run["failure"]["category"] == "mcp_call_failed"
    diagnostic_view = task_diagnostics(store, child.id, claimed.package.claim_id)
    assert diagnostic_view["ok"] is True
    failed_event = next(
        event
        for event in diagnostic_view["data"]["events"]
        if event.get("category") == "mcp_call_failed"
    )
    assert failed_event["category"] == "mcp_call_failed"
    public_detail = invocation_failure_detail(store, claimed)
    assert public_detail["status"] == "invocation_failed"
    assert public_detail["category"] == "mcp_call_failed"
    assert all(
        "private MCP failure detail" not in repr(record.payload)
        for _, record in store.scan_runtime_records()
    )
    store.close()
