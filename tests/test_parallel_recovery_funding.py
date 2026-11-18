"""Signed parallel delegation keeps only feasible parent-funded recovery capacity."""

from __future__ import annotations

import json
import shutil

import pytest

from types import SimpleNamespace

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.tools import ToolRegistry, ToolSpec
from aigineering.plugins.delegation import _parallel_tool_contracts
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.types import Contract
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_worker_failures,
)


def _environment(name: str, *, store_path=":memory:"):
    store = SQLiteStore(store_path)
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        f"test:parallel-recovery-{name}",
        "setup",
        setup_signer.kind,
        setup_signer.signer_id,
        (
            "actor.authorize",
            "actor.revoke",
            "actor.rotate",
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
            "worker.register",
        ),
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.v1",
        "recovery-v1",
        recovery_signer.kind,
        recovery_signer.signer_id,
        (
            "actor.revoke",
            "actor.rotate",
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(
        f"parallel-recovery-{name}",
        (setup_key, recovery_key),
        f"policy:parallel-recovery-{name}",
    )
    initialize_genesis(store, genesis)
    setup = candidate_runtime(
        store, genesis=genesis, actor_key=setup_key, signer=setup_signer
    )
    recovery_publisher = CandidatePublisher(
        store, store, genesis, recovery_key, recovery_signer
    )
    publishers = CandidatePublisherRegistry(
        (("recovery.publish.v1", recovery_publisher),)
    )
    return SimpleNamespace(
        store=store,
        genesis=genesis,
        setup=setup,
        publisher=recovery_publisher,
        publishers=publishers,
    )


def _proven_recoveries(env, source_ids):
    from aigineering.task_lineage import task_lineage

    targets = set()
    for source_id in source_ids:
        result = task_lineage(env.store, source_id)
        assert result["ok"], result
        targets.update(
            edge["target_contract_id"]
            for edge in result["data"]["edges"]
            if edge["kind"] == "recovery_publication"
            and edge["source_contract_id"] in source_ids
        )
    return [env.store.get_contract(task_id) for task_id in sorted(targets)]


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


def _parent(
    env,
    name: str,
    budget: int,
    tools: tuple[str, ...],
    *,
    outputs: tuple[str, ...] | None = None,
    acceptance_policy: dict | None = None,
) -> Contract:
    return env.setup.accept_contract(
        Contract(
            id=f"task:parallel-recovery:{name}",
            name=name,
            outputs=outputs or (f"{name}_report",),
            budget=budget,
            tool_scope=tools,
            acceptance_policy=acceptance_policy,
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
    # The signed root Candidate reserves the tool task and its continuation
    # before either is run, leaving one unit for the parent recovery source.
    assert allowance_available(parent, records) == 1
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
    (recovery,) = _proven_recoveries(env, {failed.id})
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
    recoveries = _proven_recoveries(env, {c.id for c in items})
    assert recoveries == []
    for item in items:
        assert env.store.read_task_facts(item.id, ("worker.invocation_failed",))
        assert env.store.read_task_facts(item.id, ("allowance.extinguished",))
    assert _claim(env, tool_host, join.id, "unfunded-join") is None
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
    recoveries = _proven_recoveries(env, {c.id for c in items})
    assert len(recoveries) == 1
    recovery = recoveries[0]
    recovery_claim = _claim(
        env, tool_host, recovery.id, "claim:parallel-recursive-recovery"
    )
    assert recovery_claim is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            recovery_claim, tool_host, env.store, candidate_publishers=env.publishers
        )
    assert _proven_recoveries(env, {recovery.id}) == []
    assert env.store.read_task_facts(recovery.id, ("worker.invocation_failed",))
    assert env.store.read_task_facts(recovery.id, ("allowance.extinguished",))
    assert _claim(env, tool_host, join.id, "claim:parallel-exhaustion-join") is None
    env.store.close()


@pytest.mark.parametrize("parent_budget", [8, 9])
def test_old_full_effective_allowance_parallel_history_replays_without_identity_change(
    monkeypatch, tmp_path, parent_budget
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
    # builder receiving its full effective allowance of 8. Its real waiting
    # join has enough remaining allowance to fund one repair in both cases.
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
    recoveries = _proven_recoveries(env, {failed.id})
    assert tuple(item.id for item in old_children) == old_ids
    assert len(recoveries) == 1
    assert recoveries[0].id not in old_ids
    assert recoveries[0].parent_id in old_ids  # Actual waiting join funds the repair.
    assert recoveries[0].budget == 1
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


def test_signed_parallel_review_funding_combines_review_and_recovery_demand(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env = _environment(
        "parallel-review-funding", store_path=tmp_path / "parallel-review.sqlite3"
    )
    policy = {
        "mode": "independent",
        "policy_version": "parallel-review-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["verify.human"],
        "rubric_asset_ids": [],
        "evidence_asset_ids": [],
    }
    parent = _parent(
        env,
        "parallel-review",
        10,
        ("a", "b"),
        outputs=("report-a", "report-b"),
        acceptance_policy=policy,
    )
    calls = [{"id": name, "name": name, "args": {"q": name}} for name in ("a", "b")]
    for name in ("a", "b"):
        env.setup.accept_asset(
            create_tool_descriptor(name, f"fixture {name}", {"type": "object"})
        )

    _, items, join = _signed_parallel(
        env, parent, calls, "worker:parallel-review-funding"
    )
    records = tuple(record for _, record in env.store.scan_runtime_records())
    assert len(items) == 2
    assert all(item.budget == 1 for item in items)
    # Two review units plus the existing two first-recovery slots stay on the
    # parent; the join retains a positive budget for its ordinary continuation.
    assert join.budget == 4
    assert allowance_available(parent, records) == 4
    env.store.close()


def test_signed_parallel_review_minimum_shares_residual_with_recovery(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env = _environment(
        "parallel-review-minimum", store_path=tmp_path / "parallel-review-min.sqlite3"
    )
    policy = {
        "mode": "independent",
        "policy_version": "parallel-review-min-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["verify.human"],
        "rubric_asset_ids": [],
        "evidence_asset_ids": [],
    }
    parent = _parent(
        env, "parallel-review-minimum", 4, ("a", "b"), acceptance_policy=policy
    )
    calls = [{"id": name, "name": name, "args": {"q": name}} for name in ("a", "b")]
    for name in ("a", "b"):
        env.setup.accept_asset(
            create_tool_descriptor(name, f"fixture {name}", {"type": "object"})
        )

    _, items, join = _signed_parallel(
        env, parent, calls, "worker:parallel-review-minimum"
    )
    records = tuple(record for _, record in env.store.scan_runtime_records())
    assert len(items) == 2
    # At this boundary one unit is the common parent balance. It can fund a
    # default review or the existing first recovery, without an ordering claim.
    assert join.budget == 1
    assert allowance_available(parent, records) == 1
    env.store.close()


def test_signed_parallel_tool_below_call_and_join_minimum_is_rejected_without_grants(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env = _environment(
        "parallel-review-below-minimum",
        store_path=tmp_path / "parallel-review-below-minimum.sqlite3",
    )
    parent = _parent(
        env,
        "parallel-review-below-minimum",
        2,
        ("a", "b"),
        acceptance_policy={
            "mode": "independent",
            "policy_version": "parallel-review-low-v1",
            "required_attestations": 1,
            "verifier_capabilities": ["verify.human"],
            "rubric_asset_ids": [],
            "evidence_asset_ids": [],
        },
    )
    for name in parent.tool_scope:
        env.setup.accept_asset(
            create_tool_descriptor(name, f"fixture {name}", {"type": "object"})
        )
    worker = MockWorker(worker_id="worker:parallel-review-below-minimum")
    worker.set_output(
        parent.name,
        '/parallel_tool {"calls":[{"id":"a","name":"a","args":{}},'
        '{"id":"b","name":"b","args":{}}]}',
    )
    host = hosted_worker(
        env.store,
        worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claim = _claim(env, host, parent.id, "claim:parallel-review-below-minimum")
    assert claim is not None

    with pytest.raises(WorkerInvocationError, match="worker invocation failed"):
        execute_claimed_package(
            claim, host, env.store, candidate_publishers=env.publishers
        )

    assert [
        item for item in env.store.get_all_contracts() if item.parent_id == parent.id
    ] == []
    records = tuple(record for _, record in env.store.scan_runtime_records())
    grants = [
        record
        for record in records
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == parent.id
    ]
    assert len(grants) == 1
    assert grants[0].payload["amount"] == 2
    assert not [
        record
        for record in records
        if record.record_type == "allowance.reserved"
        and record.payload.get("parent_contract_id") == parent.id
    ]
    failures = [
        record for record in records if record.record_type == "worker.invocation_failed"
    ]
    assert len(failures) == 1
    assert failures[0].payload["category"] == "worker_error:task_delegation_rejected"
    terminal = [
        record for record in records if record.record_type == "lifecycle.terminal"
    ]
    assert terminal == []
    assert env.store.read_task_facts(parent.id, ("allowance.extinguished",))
    env.store.close()
