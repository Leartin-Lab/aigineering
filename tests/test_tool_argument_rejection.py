"""Durable rejection of malformed ToolWorker arguments."""

from __future__ import annotations

import json

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.plugins.task_semantics import method_contract, system_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.actions import parse_action
from aigineering.protocol.types import Contract, ToolSpec
from aigineering.runtime import claim_next_package, execute_claimed_package


def test_malformed_tool_args_are_rejected_durably(tmp_path) -> None:
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

        result = execute_claimed_package(claimed, host, store)

        assert result["status"] == "accepted", result
        assert calls == []
        observation = json.loads(store.get_assets_by_name(child.outputs[0])[0].content)
        assert observation["ok"] is False
        assert observation["error_type"] == "ToolActionError"
        assert "JSON object" in observation["error"]
    finally:
        store.close()

    reopened = SQLiteStore(str(db_path))
    try:
        before_digest = reopened.runtime_materialization_digest()
        assert reopened.rebuild_runtime_materializations() == before_digest
        observation = json.loads(
            reopened.get_assets_by_name(child.outputs[0])[0].content
        )
        assert observation["ok"] is False
        assert observation["error_type"] == "ToolActionError"
        assert "JSON object" in observation["error"]
    finally:
        reopened.close()
