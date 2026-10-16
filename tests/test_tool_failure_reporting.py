"""Signed tool failures appear in task status without exposing raw results."""

from __future__ import annotations

import json

import pytest

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.agent.tool_worker import ToolWorker
from aigineering.runtime import WorkerInvocationError
from aigineering.run_failure_diagnostics import invocation_failure_detail
from aigineering.task_diagnostics import task_diagnostics, task_runs
from aigineering.application import default_completion_registry
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract, ToolSpec
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    process_task_completions,
)


def test_signed_tool_schema_failure_is_local_and_commits_no_observation(
    tmp_path,
) -> None:
    db_path = tmp_path / "tool-failure-report.db"
    store = SQLiteStore(str(db_path))
    secret_result = "sealed-tool-result-marker"
    try:
        ingress = candidate_runtime(store)
        root = ingress.accept_contract(
            Contract(
                id="task:tool-failure-report",
                name="tool_failure_report",
                outputs=("report",),
                tool_scope=("lookup", "schema_bad"),
                budget=4,
            )
        )
        output_schema = {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        }
        for name in ("lookup", "schema_bad"):
            descriptor = create_tool_descriptor(
                name,
                f"{name} fixture",
                {},
                output_schema=output_schema,
                source_uri=f"python:{name}",
            )
            ingress.accept_asset(descriptor, source="capability", allow_protected=True)

        root_worker = MockWorker(worker_id="worker:root")
        root_host = hosted_worker(
            store,
            root_worker,
            genesis=ingress.genesis,
            authority_key=ingress.actor_key,
            authority_signer=ingress.signer,
        )
        root_worker.set_output(root.name, '/tool {"name":"lookup","args":{}}')
        root_claim = claim_next_package(
            store, worker_id=root_host.worker_id, contract_id=root.id
        )
        assert root_claim is not None
        delegated = execute_claimed_package(root_claim, root_host, store)
        assert delegated["status"] == "task_delegated"
        successful_tool = store.get_contract(delegated["child_contract_id"])
        assert successful_tool is not None

        registry = ToolRegistry()
        registry.register(
            ToolSpec(name="lookup", output_schema=output_schema),
            lambda _args: json.dumps({"value": "safe"}),
        )
        registry.register(
            ToolSpec(name="schema_bad", output_schema=output_schema),
            lambda _args: json.dumps({"other": secret_result}),
        )
        tool_host = hosted_worker(
            store,
            ToolWorker(registry, worker_id="tool_worker:local"),
            genesis=ingress.genesis,
            authority_key=ingress.actor_key,
            authority_signer=ingress.signer,
        )
        publishers = CandidatePublisherRegistry(
            (("continuation.publish.v1", ingress.publisher),)
        )
        successful_claim = claim_next_package(
            store, worker_id=tool_host.worker_id, contract_id=successful_tool.id
        )
        assert successful_claim is not None
        assert (
            execute_claimed_package(successful_claim, tool_host, store)["status"]
            == "accepted"
        )
        assert process_task_completions(
            store,
            default_completion_registry(),
            candidate_publishers=publishers,
        ) == [successful_tool.id]

        continuation = next(
            contract
            for contract in store.get_all_contracts()
            if contract.origin == "continuation" and contract.parent_id == root.id
        )
        root_worker.set_output(
            continuation.name,
            '/tool {"name":"schema_bad","args":{}}',
        )
        continuation_claim = claim_next_package(
            store, worker_id=root_host.worker_id, contract_id=continuation.id
        )
        assert continuation_claim is not None
        delegated_failure = execute_claimed_package(
            continuation_claim, root_host, store
        )
        assert delegated_failure["status"] == "task_delegated"
        failed_tool = store.get_contract(delegated_failure["child_contract_id"])
        assert failed_tool is not None

        claim_request_id = "signed-tool-schema-failure-claim"
        claim_proposal = tool_host.claim_candidate(
            request_id=claim_request_id, contract_id=failed_tool.id
        )
        failed_claim = claim_next_package(
            store,
            worker_id=tool_host.worker_id,
            contract_id=failed_tool.id,
            claim_candidate=claim_proposal,
        )
        assert failed_claim is not None
        request_record = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="worker.claim.requested"
            )
            if record.payload.get("idempotency_key") == claim_request_id
        )
        receipt_record = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("candidate_id") == claim_proposal.id
        )
        grant_record = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("claim_id") == failed_claim.package.claim_id
        )
        assert request_record.payload["candidate_id"] == claim_proposal.id
        assert request_record.causal_parents == (receipt_record.id,)
        assert grant_record.payload["contract_id"] == failed_tool.id
        assert grant_record.causal_parents == (request_record.id,)
        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(failed_claim, tool_host, store)

        assert store.get_assets_by_name(failed_tool.outputs[0]) == []
        failure_record = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
            if record.payload.get("contract_id") == failed_tool.id
        )
        assert failure_record.payload["category"] == "worker_error:tool_schema_invalid"
        assert (
            failure_record.payload["diagnostic"] == "Tool input or output is invalid."
        )
        run_view = task_runs(store, failed_tool.id)
        assert run_view["ok"] is True
        failed_run = next(
            run
            for run in run_view["data"]["runs"]
            if run["claim_id"] == failed_claim.package.claim_id
        )
        assert failed_run["failure"]["category"] == "tool_schema_invalid"
        diagnostic_view = task_diagnostics(
            store, failed_tool.id, failed_claim.package.claim_id
        )
        assert diagnostic_view["ok"] is True
        failed_event = next(
            event
            for event in diagnostic_view["data"]["events"]
            if event.get("category") == "tool_schema_invalid"
        )
        assert failed_event["category"] == "tool_schema_invalid"
        public_detail = invocation_failure_detail(store, failed_claim)
        assert public_detail["status"] == "invocation_failed"
        assert public_detail["category"] == "tool_schema_invalid"

        records = [record for _, record in store.scan_runtime_records()]
        serialized_facts = json.dumps(
            [deep_thaw(record.payload) for record in records], sort_keys=True
        )
        assert secret_result not in serialized_facts
        assert root.id not in {
            record.payload.get("contract_id")
            for _, record in store.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
        }
    finally:
        store.close()
