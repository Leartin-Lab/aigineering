"""Signed parallel delegation keeps only feasible parent-funded recovery capacity."""

from __future__ import annotations

import json
import shutil

import pytest

from conftest import hosted_worker
from test_recovery_publication_history import _environment

from aigineering.agent.mock import MockWorker
from aigineering.agent.tool_worker import ToolWorker
from aigineering.application import default_completion_registry
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry, ToolSpec
from aigineering.plugins.delegation import _parallel_tool_contracts
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Contract
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_task_completions,
    process_worker_failures,
)


def _claim(env, host, contract_id: str, request_id: str):
    return claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=contract_id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id=request_id, contract_id=contract_id
        ),
    )


def _signed_parallel(env, parent: Contract, calls: list[dict], worker_id: str):
    worker = MockWorker(worker_id=worker_id)
    worker.set_output(
        parent.name,
        "/parallel_tool " + json.dumps({"calls": calls}, sort_keys=True),
    )
    host = hosted_worker(
        env.store,
        worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claim = _claim(env, host, parent.id, f"claim:{worker_id}:parent")
    assert claim is not None
    result = execute_claimed_package(
        claim, host, env.store, candidate_publishers=env.publishers
    )
    assert result["status"] == "task_delegated", result
    declarations = [
        item for item in env.store.get_all_contracts() if item.parent_id == parent.id
    ]
    items = [
        item
        for item in declarations
        if method_payload(item).get("method") == "parallel_tool_item"
    ]
    join = next(item for item in declarations if item.origin == "continuation")
    return host, items, join


def _parent(env, name: str, budget: int, tools: tuple[str, ...]) -> Contract:
    return env.setup.accept_contract(
        Contract(
            id=f"task:parallel-recovery:{name}",
            name=name,
            outputs=(f"{name}_report",),
            budget=budget,
            tool_scope=tools,
        )
    )


def _register_tools(env, names: tuple[str, ...], handlers: dict[str, object]):
    registry = ToolRegistry()
    for name in names:
        env.setup.accept_asset(
            create_tool_descriptor(name, f"fixture {name}", {"type": "object"})
        )
        registry.register(
            ToolSpec(name=name, input_schema={"type": "object"}), handlers[name]
        )
    worker = ToolWorker(registry, worker_id="worker:parallel-recovery-tools")
    host = hosted_worker(
        env.store,
        worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    return host


@pytest.mark.parametrize(
    ("count", "allowance", "expected_reserve"),
    [(2, 3, 0), (2, 4, 1), (2, 8, 2), (8, 9, 0), (8, 17, 8)],
)
def test_signed_parallel_allowance_thresholds(
    monkeypatch, tmp_path, count, allowance, expected_reserve
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    database = tmp_path / f"signed-parallel-{count}-{allowance}.sqlite3"
    env = _environment(f"parallel-budget-{count}-{allowance}", store_path=database)
    names = tuple(f"tool{index}" for index in range(count))
    parent = _parent(env, f"parallel-{count}-{allowance}", allowance, names)
    calls = [
        {"id": f"call-{index}", "name": name, "args": {"index": index}}
        for index, name in enumerate(names)
    ]
    for name in names:
        env.setup.accept_asset(
            create_tool_descriptor(name, f"fixture {name}", {"type": "object"})
        )
    _, items, join = _signed_parallel(
        env, parent, calls, f"worker:parallel-budget-{count}-{allowance}"
    )
    reserve = min(count, max(0, allowance - count - 1))
    records = [record for _, record in env.store.scan_runtime_records()]
    assert reserve == expected_reserve
    assert len(items) == count
    assert all(item.budget == 1 for item in items)
    assert join.budget == allowance - count - reserve
    assert allowance_available(parent, records) == reserve
    assert tuple(method_payload(item)["payload"]["args"] for item in items) == tuple(
        {"index": index} for index in range(count)
    )
    env.store.close()


def test_signed_tool_control_keeps_parent_recovery_balance(monkeypatch, tmp_path):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env = _environment("single-tool-budget", store_path=tmp_path / "single.sqlite3")
    parent = _parent(env, "single-tool", 4, ("lookup",))
    env.setup.accept_asset(
        create_tool_descriptor("lookup", "fixture lookup", {"type": "object"})
    )
    worker = MockWorker(worker_id="worker:single-tool-parent")
    worker.set_output(parent.name, '/tool {"name":"lookup","args":{"q":"x"}}')
    host = hosted_worker(
        env.store,
        worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claim = _claim(env, host, parent.id, "claim:single-tool-parent")
    assert claim is not None
    execute_claimed_package(claim, host, env.store, candidate_publishers=env.publishers)
    child = next(
        item
        for item in env.store.get_all_contracts()
        if item.parent_id == parent.id and method_payload(item).get("method") == "tool"
    )
    assert child.budget == 1
    records = [record for _, record in env.store.scan_runtime_records()]
    assert allowance_available(parent, records) == 3
    env.store.close()


def test_signed_parallel_one_failure_recovery_healthy_sibling_and_join(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    database = tmp_path / "parallel-recovery-success.sqlite3"
    env = _environment("parallel-recovery-success", store_path=database)
    parent = _parent(env, "parallel-success", 8, ("fragile", "healthy"))
    calls = {"fragile": 0, "healthy": 0}

    def fragile(args):
        calls["fragile"] += 1
        if calls["fragile"] == 1:
            raise TimeoutError("private fixture timeout")
        return json.dumps({"found": False, "matches": []}, sort_keys=True)

    def healthy(args):
        calls["healthy"] += 1
        return json.dumps({"found": False, "matches": []}, sort_keys=True)

    tool_host = _register_tools(
        env,
        ("fragile", "healthy"),
        {
            "fragile": fragile,
            "healthy": healthy,
        },
    )
    root_host, items, join = _signed_parallel(
        env,
        parent,
        [
            {"id": "fragile", "name": "fragile", "args": {"q": "x"}},
            {"id": "healthy", "name": "healthy", "args": {"q": "n"}},
        ],
        "worker:parallel-success-parent",
    )
    failed = next(
        item for item in items if method_payload(item)["parallel_call_id"] == "fragile"
    )
    sibling = next(
        item for item in items if method_payload(item)["parallel_call_id"] == "healthy"
    )
    sibling_claim = _claim(env, tool_host, sibling.id, "claim:parallel-success-sibling")
    assert sibling_claim is not None
    assert (
        execute_claimed_package(
            sibling_claim, tool_host, env.store, candidate_publishers=env.publishers
        )["status"]
        == "accepted"
    )
    sibling_output = env.store.get_assets_by_name(sibling.outputs[0])
    assert len(sibling_output) == 1
    assert json.loads(json.loads(sibling_output[0].content)["result"])["found"] is False

    failed_claim = _claim(env, tool_host, failed.id, "claim:parallel-success-failed")
    assert failed_claim is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            failed_claim, tool_host, env.store, candidate_publishers=env.publishers
        )
    assert env.store.get_assets_by_name(failed.outputs[0]) == []
    recovery = next(
        item
        for item in env.store.get_all_contracts()
        if method_payload(item).get("recovery_source_contract_id") == failed.id
    )
    assert method_payload(recovery)["payload"] == method_payload(failed)["payload"]
    assert _claim(env, root_host, join.id, "claim:parallel-early-join") is None
    recovery_claim = _claim(env, tool_host, recovery.id, "claim:parallel-recovery")
    assert recovery_claim is not None
    assert (
        execute_claimed_package(
            recovery_claim, tool_host, env.store, candidate_publishers=env.publishers
        )["status"]
        == "accepted"
    )
    observed = env.store.get_assets_by_name(failed.outputs[0])
    assert len(observed) == 1
    assert observed[0].created_by == recovery.id
    assert observed[0].origin == "worker"
    assert calls == {"fragile": 2, "healthy": 1}

    assert process_task_completions(
        env.store,
        default_completion_registry(),
        candidate_publishers=CandidatePublisherRegistry(
            (("continuation.publish.v1", env.publisher),)
        ),
    )
    join_host_worker = MockWorker(worker_id="worker:parallel-success-join")
    join_host_worker.set_output(
        join.name,
        f'/exec {{"outputs":{{"{parent.outputs[0]}":"parallel complete"}}}}',
    )
    join_host = hosted_worker(
        env.store,
        join_host_worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    join_claim = _claim(env, join_host, join.id, "claim:parallel-success-join")
    assert join_claim is not None
    assert (
        execute_claimed_package(
            join_claim, join_host, env.store, candidate_publishers=env.publishers
        )["status"]
        == "accepted"
    )
    from aigineering.cli.task_state import project_task_status

    assert project_task_status(parent, env.store)["status"] == "completed"
    env.store.close()


def test_signed_parallel_minimum_allowance_failure_is_unavailable(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env = _environment(
        "parallel-recovery-minimum-failure", store_path=tmp_path / "minimum.sqlite3"
    )
    parent = _parent(env, "parallel-minimum", 3, ("a", "b"))

    def always_fail(_args):
        raise RuntimeError("private minimum fixture failure")

    tool_host = _register_tools(env, ("a", "b"), {"a": always_fail, "b": always_fail})
    _, items, join = _signed_parallel(
        env,
        parent,
        [
            {"id": "a", "name": "a", "args": {}},
            {"id": "b", "name": "b", "args": {}},
        ],
        "worker:parallel-minimum-parent",
    )
    assert join.budget == 1
    for index, item in enumerate(items):
        claim = _claim(env, tool_host, item.id, f"claim:parallel-minimum-{index}")
        assert claim is not None
        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(
                claim, tool_host, env.store, candidate_publishers=env.publishers
            )
    recoveries = [
        item
        for item in env.store.get_all_contracts()
        if method_payload(item).get("recovery_source_contract_id")
        in {c.id for c in items}
    ]
    assert recoveries == []
    unavailable = [
        record
        for _, record in env.store.scan_runtime_records()
        if record.record_type == "worker_failure.recovery_unavailable"
        and record.payload.get("contract_id") in {c.id for c in items}
    ]
    assert len(unavailable) == 2
    assert {record.payload["reason"] for record in unavailable} == {
        "recovery_publication_rejected"
    }
    env.store.close()


def test_signed_parallel_capacity_exhaustion_is_observable_and_recursive_is_bounded(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env = _environment(
        "parallel-recovery-exhaustion", store_path=tmp_path / "exhaustion.sqlite3"
    )
    parent = _parent(env, "parallel-exhaustion", 4, ("a", "b"))

    def always_fail(_args):
        raise RuntimeError("private fixture failure")

    tool_host = _register_tools(env, ("a", "b"), {"a": always_fail, "b": always_fail})
    _, items, join = _signed_parallel(
        env,
        parent,
        [
            {"id": "a", "name": "a", "args": {}},
            {"id": "b", "name": "b", "args": {}},
        ],
        "worker:parallel-exhaustion-parent",
    )
    for index, item in enumerate(items):
        claim = _claim(env, tool_host, item.id, f"claim:parallel-exhaustion-{index}")
        assert claim is not None
        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(
                claim, tool_host, env.store, candidate_publishers=env.publishers
            )
    recoveries = [
        item
        for item in env.store.get_all_contracts()
        if method_payload(item).get("recovery_source_contract_id")
        in {c.id for c in items}
    ]
    assert len(recoveries) == 1
    unavailable = [
        record
        for _, record in env.store.scan_runtime_records()
        if record.record_type == "worker_failure.recovery_unavailable"
        and record.payload.get("reason") == "recovery_publication_rejected"
    ]
    assert len(unavailable) == 1
    recovery = recoveries[0]
    recovery_claim = _claim(
        env, tool_host, recovery.id, "claim:parallel-recursive-recovery"
    )
    assert recovery_claim is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            recovery_claim, tool_host, env.store, candidate_publishers=env.publishers
        )
    recursive_unavailable = [
        record
        for _, record in env.store.scan_runtime_records()
        if record.record_type == "worker_failure.recovery_unavailable"
        and record.payload.get("contract_id") == recovery.id
    ]
    assert len(recursive_unavailable) == 1
    assert recursive_unavailable[0].payload["reason"] == "recovery_publication_rejected"
    assert _claim(env, tool_host, join.id, "claim:parallel-exhaustion-join") is None
    env.store.close()


@pytest.mark.parametrize(("parent_budget", "expect_recovery"), [(8, False), (9, True)])
def test_old_full_effective_allowance_parallel_history_replays_without_identity_change(
    monkeypatch, tmp_path, parent_budget, expect_recovery
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    database = tmp_path / "old-parallel-full-allowance.sqlite3"
    env = _environment(
        f"old-parallel-full-effective-{parent_budget}", store_path=database
    )
    parent = _parent(env, "old-parallel", parent_budget, ("legacy-a", "legacy-b"))
    for name in parent.tool_scope:
        env.setup.accept_asset(
            create_tool_descriptor(name, f"fixture {name}", {"type": "object"})
        )
    action = WorkerAction(
        type="parallel_tool",
        payload={
            "calls": [
                {"id": "a", "name": "legacy-a", "args": {"q": 1}},
                {"id": "b", "name": "legacy-b", "args": {"q": 2}},
            ]
        },
    )
    # Reproduce the old accepted source shape with the unchanged canonical
    # builder receiving its full effective allowance of 8. The A=8 row has no
    # parent spare; A=9 leaves one unrelated parent unit for a successful G1
    # recovery publication.
    old_children = _parallel_tool_contracts(parent, action, 8)
    old_ids = tuple(item.id for item in old_children)
    published = env.setup.publisher.publish(
        tuple(
            __import__(
                "aigineering.protocol.effect_builders",
                fromlist=["contract_declaration_effect"],
            ).contract_declaration_effect(item)
            for item in old_children
        ),
        idempotency_key="old-full-allowance-parallel-batch",
    )
    assert published.accepted
    failed = next(
        item
        for item in published.contracts
        if method_payload(item).get("parallel_call_id") == "a"
    )

    def sometimes_fail(args):
        if args["q"] == 1:
            raise TimeoutError("private legacy failure")
        return json.dumps({"ok": True})

    tool_host = _register_tools(
        env,
        parent.tool_scope,
        {
            "legacy-a": sometimes_fail,
            "legacy-b": lambda _args: json.dumps({"ok": True}),
        },
    )
    failed_claim = _claim(env, tool_host, failed.id, "claim:old-full-parallel-failed")
    assert failed_claim is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            failed_claim, tool_host, env.store, candidate_publishers=env.publishers
        )
    recoveries = [
        item
        for item in env.store.get_all_contracts()
        if method_payload(item).get("recovery_source_contract_id") == failed.id
    ]
    assert tuple(item.id for item in old_children) == old_ids
    if expect_recovery:
        assert len(recoveries) == 1
        assert recoveries[0] is not failed
    else:
        assert recoveries == []
        outcome = next(
            record
            for _, record in env.store.scan_runtime_records()
            if record.record_type == "worker_failure.recovery_unavailable"
            and record.payload.get("contract_id") == failed.id
        )
        assert outcome.payload["reason"] == "recovery_publication_rejected"
    assert process_worker_failures(env.store, candidate_publishers=env.publishers) == []
    digest = env.store.runtime_materialization_digest()
    records = tuple(env.store.scan_runtime_records())
    env.store.close()
    reopened = SQLiteStore(database)
    assert reopened.runtime_materialization_digest() == digest
    copied_path = tmp_path / "old-parallel-copy.sqlite3"
    shutil.copy2(database, copied_path)
    copied = SQLiteStore(copied_path)
    assert copied.rebuild_runtime_materializations(strict=True) == digest
    assert copied.runtime_materialization_digest() == digest
    assert tuple(copied.scan_runtime_records()) == records
    copied.close()
    reopened.close()
