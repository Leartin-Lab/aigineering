"""Durable rejection of malformed ToolWorker arguments."""

from __future__ import annotations

import pytest

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.plugins.task_semantics import method_contract, system_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.actions import parse_action
from aigineering.protocol.types import Contract, ToolSpec
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
)


def test_malformed_tool_args_fail_without_observation_or_handler_call(tmp_path) -> None:
    db_path = tmp_path / "tool-arguments.db"
    store = SQLiteStore(str(db_path))
    calls: list[dict] = []
    try:
        ingress = candidate_runtime(store)
        parent = ingress.accept_contract(
            Contract(
                id="task:tool-argument-parent",
                name="tool-argument-parent",
                outputs=("report",),
                tool_scope=("lookup",),
                budget=2,
            )
        )
        descriptor = create_tool_descriptor(
            "lookup",
            "Lookup values",
            {},
            source_uri="python:lookup",
        )
        ingress.accept_asset(descriptor, source="capability", allow_protected=True)

        action_text = '/tool {"name":"lookup","args":"malformed"}'
        child = ingress.accept_contract(
            method_contract(parent, parse_action(action_text))
        )
        ingress.accept_asset(
            system_asset(
                name=f"_method_ctx_{parent.id}",
                content=action_text,
                created_by=parent.id,
            ),
            source="method",
            allow_protected=True,
        )

        registry = ToolRegistry()
        registry.register(
            ToolSpec(name="lookup"),
            lambda args: calls.append(dict(args)) or "unexpected success",
        )
        worker = ToolWorker(registry, worker_id="tool_worker:local")
        host = hosted_worker(
            store,
            worker,
            genesis=ingress.genesis,
            authority_key=ingress.actor_key,
            authority_signer=ingress.signer,
        )
        claimed = claim_next_package(
            store, worker_id=host.worker_id, contract_id=child.id
        )
        assert claimed is not None

        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(claimed, host, store)
        assert calls == []
        assert store.get_assets_by_name(child.outputs[0]) == []
        failure = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
            if record.payload.get("contract_id") == child.id
        )
        assert failure.payload["category"] == "worker_error:tool_arguments_invalid"
    finally:
        store.close()

    reopened = SQLiteStore(str(db_path))
    try:
        before_digest = reopened.runtime_materialization_digest()
        assert reopened.rebuild_runtime_materializations() == before_digest
        assert reopened.get_assets_by_name(child.outputs[0]) == []
        assert any(
            record.payload.get("contract_id") == child.id
            for _, record in reopened.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
        )
    finally:
        reopened.close()
