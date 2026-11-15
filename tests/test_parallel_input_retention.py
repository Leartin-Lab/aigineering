"""Parallel continuations retain disclosed source evidence across tool stages."""

from __future__ import annotations

import json
import shutil

from aigineering.agent.mock import MockWorker
from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry, ToolSpec
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package, execute_claimed_package
from conftest import candidate_runtime, hosted_worker


class _RecordingWorker(MockWorker):
    def __init__(self, worker_id: str) -> None:
        super().__init__(worker_id=worker_id)
        self.disclosed: tuple[tuple[str, str], ...] = ()

    def invoke(self, contract, disclosed_assets):
        self.disclosed = tuple(
            (asset.name, asset.content) for asset in disclosed_assets
        )
        return super().invoke(contract, disclosed_assets)


def _claim_and_execute(store, host, contract: Contract, request_id: str):
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        claim_candidate=host.claim_candidate(
            request_id=request_id, contract_id=contract.id
        ),
    )
    return claimed


def _execute(store, host, contract: Contract, request_id: str) -> dict:
    claimed = _claim_and_execute(store, host, contract, request_id)
    assert claimed is not None, contract.id
    return execute_claimed_package(claimed, host, store)


def test_parallel_join_discloses_original_inputs_and_waits_for_all_observations(
    tmp_path,
):
    database = tmp_path / "parallel-input-retention.sqlite3"
    store = SQLiteStore(str(database))
    setup = candidate_runtime(store)

    original = {
        "co2_data": '{"samples":[412,413]}',
        "study_spec": '{"window":"summer-2025"}',
        "audit": '{"source":"NOAA","revision":"frozen-1"}',
    }
    for name, content in original.items():
        setup.accept_asset(build_control_plane_asset(name=name, content=content))

    names = ("lookup", "temperature", "humidity")
    descriptors = {
        name: create_tool_descriptor(name, f"fixture {name}", {"type": "object"})
        for name in names
    }
    for descriptor in descriptors.values():
        setup.accept_asset(descriptor)

    policy = {"mode": "mechanical"}
    parent = setup.accept_contract(
        Contract(
            id="task:parallel-input-retention",
            name="noaa-study",
            description="Preserve source and audit evidence while gathering observations.",
            inputs=tuple(original),
            outputs=("report",),
            budget=12,
            tool_scope=names,
            labels=("research:noaa",),
            acceptance_policy=policy,
        )
    )

    root_worker = MockWorker(worker_id="worker:noaa-single-tool")
    root_worker.set_output(
        parent.name, '/tool {"name":"lookup","args":{"query":"baseline"}}'
    )
    root_host = hosted_worker(
        store,
        root_worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    assert _execute(store, root_host, parent, "claim:noaa-single-tool")["status"] == (
        "task_delegated"
    )

    declarations = store.get_all_contracts()
    first_tool = next(
        task
        for task in declarations
        if task.parent_id == parent.id and method_payload(task).get("method") == "tool"
    )
    first_continuation = next(
        task
        for task in declarations
        if task.parent_id == parent.id and task.origin == "continuation"
    )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="lookup", input_schema={"type": "object"}),
        lambda args: json.dumps({"baseline": args["query"]}),
    )
    registry.register(
        ToolSpec(name="temperature", input_schema={"type": "object"}),
        lambda args: json.dumps({"celsius": args["station"]}),
    )
    registry.register(
        ToolSpec(name="humidity", input_schema={"type": "object"}),
        lambda args: json.dumps({"percent": args["station"]}),
    )
    tool_worker = ToolWorker(registry, worker_id="worker:noaa-observations")
    tool_host = hosted_worker(
        store,
        tool_worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    assert (
        _execute(store, tool_host, first_tool, "claim:noaa-first-observation")["status"]
        == "accepted"
    )

    first_observation = store.get_assets_by_name(first_tool.outputs[0])
    assert len(first_observation) == 1
    assert json.loads(first_observation[0].content)["ok"] is True

    first_continuation_worker = MockWorker(worker_id="worker:noaa-parallel-request")
    first_continuation_worker.set_output(
        first_continuation.name,
        '/parallel_tool {"calls":[{"id":"temp","name":"temperature",'
        '"args":{"station":"station-1"}},{"id":"humidity","name":"humidity",'
        '"args":{"station":"station-1"}}],"join":"all"}',
    )
    continuation_host = hosted_worker(
        store,
        first_continuation_worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    assert (
        _execute(
            store,
            continuation_host,
            first_continuation,
            "claim:noaa-parallel-request",
        )["status"]
        == "task_delegated"
    )

    declarations = store.get_all_contracts()
    siblings = [
        task
        for task in declarations
        if task.parent_id == first_continuation.id
        and method_payload(task).get("method") == "parallel_tool_item"
    ]
    join = next(
        task
        for task in declarations
        if task.parent_id == first_continuation.id and task.origin == "continuation"
    )
    assert len(siblings) == 2
    sibling_observations = {task.outputs[0] for task in siblings}
    observation_names = tuple(join.activation.split(" AND "))
    assert set(observation_names) == sibling_observations
    assert join.inputs == tuple(
        dict.fromkeys((*first_continuation.inputs, *observation_names))
    )
    assert join.activation == " AND ".join(observation_names)
    assert join.tool_scope == first_continuation.tool_scope == parent.tool_scope
    assert join.labels == tuple(
        dict.fromkeys((*first_continuation.labels, "plugin:parallel_tool.continuation"))
    )
    assert (
        join.acceptance_policy
        == first_continuation.acceptance_policy
        == parent.acceptance_policy
    )

    join_worker = _RecordingWorker("worker:noaa-final-join")
    join_worker.set_output(join.name, '/exec {"outputs":{"report":"joined"}}')
    join_host = hosted_worker(
        store,
        join_worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    assert _claim_and_execute(store, join_host, join, "claim:noaa-early-join") is None

    for index, sibling in enumerate(siblings):
        assert (
            _execute(
                store,
                tool_host,
                sibling,
                f"claim:noaa-parallel-observation-{index}",
            )["status"]
            == "accepted"
        )
        if index == 0:
            assert (
                _claim_and_execute(
                    store, join_host, join, "claim:noaa-join-after-one-observation"
                )
                is None
            )

    join_result = _execute(store, join_host, join, "claim:noaa-ready-join")
    assert join_result["status"] == "accepted", join_result
    disclosed = dict(join_worker.disclosed)
    assert {name: disclosed[name] for name in original} == original
    assert first_tool.outputs[0] in disclosed
    assert sibling_observations.issubset(disclosed)
    assert set(join.inputs).issubset(disclosed)
    assert disclosed[first_tool.outputs[0]] == first_observation[0].content
    for observation_name in sibling_observations:
        observed_assets = store.get_assets_by_name(observation_name)
        assert len(observed_assets) == 1
        assert disclosed[observation_name] == observed_assets[0].content
    method_context_names = {
        name for name in disclosed if name.startswith("_method_ctx_")
    }
    assert set(disclosed).difference(join.inputs) == method_context_names
    assert len(join_worker.disclosed) == len(disclosed)
    digest = store.runtime_materialization_digest()
    records = tuple(store.scan_runtime_records())
    store.close()

    reopened = SQLiteStore(str(database))
    assert reopened.runtime_materialization_digest() == digest
    assert tuple(reopened.scan_runtime_records()) == records
    reopened.close()

    copied_database = tmp_path / "parallel-input-retention-rebuild.sqlite3"
    shutil.copy2(database, copied_database)
    rebuilt = SQLiteStore(str(copied_database))
    assert rebuilt.runtime_materialization_digest() == digest
    assert rebuilt.rebuild_runtime_materializations(strict=True) == digest
    assert tuple(rebuilt.scan_runtime_records()) == records
    rebuilt.close()
