"""Repeated tool repairs use only real waiting-work allowance."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry, ToolSpec
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.types import Contract
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_worker_failures,
)


def _world(tmp_path):
    database_path = tmp_path / "tool-recovery-funding.sqlite3"
    store = SQLiteStore(database_path)
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        "human:tool-recovery-test",
        "key:setup",
        setup_signer.kind,
        setup_signer.signer_id,
        (
            "actor.authorize",
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
        "key:recovery",
        recovery_signer.kind,
        recovery_signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(
        "tool-recovery-funding", (setup_key, recovery_key), "policy:test"
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
    parent = setup.accept_contract(
        Contract(
            id="task:tool-recovery-root",
            name="tool-recovery-root",
            outputs=("final_report",),
            budget=5,
            tool_scope=("fragile_lookup",),
        )
    )
    setup.accept_asset(
        create_tool_descriptor("fragile_lookup", "fixture lookup", {"type": "object"})
    )

    planner_worker = MockWorker(worker_id="worker:plan-tool")
    planner_worker.set_output(
        parent.name, '/tool {"name":"fragile_lookup","args":{"q":"x"}}'
    )
    planner_host = hosted_worker(
        store,
        planner_worker,
        genesis=genesis,
        authority_key=setup_key,
        authority_signer=setup_signer,
    )
    parent_claim = claim_next_package(
        store,
        worker_id=planner_host.worker_id,
        contract_id=parent.id,
        candidate_publishers=publishers,
        claim_candidate=planner_host.claim_candidate(
            request_id="claim:tool-root", contract_id=parent.id
        ),
    )
    assert parent_claim is not None
    result = execute_claimed_package(
        parent_claim, planner_host, store, candidate_publishers=publishers
    )
    assert result["status"] == "task_delegated", result
    source = next(
        item
        for item in store.get_all_contracts()
        if item.parent_id == parent.id and method_payload(item).get("method") == "tool"
    )
    consumer = next(
        item
        for item in store.get_all_contracts()
        if item.parent_id == parent.id and item.origin == "continuation"
    )
    # Consume the root's retained unit through a real signed child. Repairs must
    # be paid by the waiting consumer and preserve its final unit.
    setup.accept_contract(
        Contract(
            id="task:tool-recovery-root-reserve",
            name="root-reserve-consumer",
            parent_id=parent.id,
            outputs=("reserve_result",),
            budget=1,
        )
    )

    registry = ToolRegistry()

    def fail_tool(_args):
        raise TimeoutError("fixture tool failure")

    registry.register(
        ToolSpec(name="fragile_lookup", input_schema={"type": "object"}), fail_tool
    )
    worker = ToolWorker(registry, worker_id="worker:fragile-lookup")
    tool_host = hosted_worker(
        store,
        worker,
        genesis=genesis,
        authority_key=setup_key,
        authority_signer=setup_signer,
    )
    return SimpleNamespace(
        store=store,
        database_path=database_path,
        genesis=genesis,
        setup=setup,
        recovery_key=recovery_key,
        recovery_signer=recovery_signer,
        parent=parent,
        source=source,
        consumer=consumer,
        host=tool_host,
        publishers=publishers,
    )


def _claim(env, contract, request_id):
    return claim_next_package(
        env.store,
        worker_id=env.host.worker_id,
        contract_id=contract.id,
        candidate_publishers=env.publishers,
        claim_candidate=env.host.claim_candidate(
            request_id=request_id, contract_id=contract.id
        ),
    )


def _fail_once(env, contract, request_id):
    claim = _claim(env, contract, request_id)
    assert claim is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            claim, env.host, env.store, candidate_publishers=env.publishers
        )
    failure = next(
        record
        for _revision, record in reversed(env.store.scan_runtime_records())
        if record.record_type == "worker.invocation_failed"
        and record.payload.get("contract_id") == contract.id
        and record.payload.get("claim_id") == claim.package.claim_id
    )
    return claim, failure


def test_repeated_tool_recovery_spends_waiting_consumer_once_per_failure(
    tmp_path, monkeypatch
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env = _world(tmp_path)
    root_records = tuple(record for _, record in env.store.scan_runtime_records())
    assert allowance_available(env.parent, root_records) == 0
    assert allowance_available(env.consumer, root_records) == 3

    _source_claim, source_failure = _fail_once(env, env.source, "claim:tool-source")
    from aigineering.task_diagnostics import (
        TaskDiagnosticError,
        task_recovery_source_binding,
    )
    from aigineering.task_view_query import load_task_view_query_context

    source_query = load_task_view_query_context(env.store)
    source_binding = task_recovery_source_binding(
        env.store,
        env.source.id,
        _source_claim.package.claim_id,
        source_failure.id,
        source_failure.record_type,
        query_context=source_query,
    )
    assert source_binding["claim_id"] == _source_claim.package.claim_id
    assert source_failure.id in source_binding["witness_record_ids"]
    with pytest.raises(TaskDiagnosticError):
        task_recovery_source_binding(
            env.store,
            env.source.id,
            "claim:forged",
            source_failure.id,
            source_failure.record_type,
            query_context=source_query,
        )
    recoveries = [
        item
        for item in env.store.get_all_contracts()
        if item.parent_id == env.consumer.id and item.outputs == env.source.outputs
    ]
    assert len(recoveries) == 1
    recovery_one = recoveries[0]
    assert recovery_one.budget == 1
    assert recovery_one.tool_scope == env.source.tool_scope
    assert (
        allowance_available(
            env.consumer, [record for _, record in env.store.scan_runtime_records()]
        )
        == 2
    )
    assert _recovery_receipt_proves(
        env, recovery_one, env.source, env.source.id, source_failure.id
    )

    from concurrent.futures import ThreadPoolExecutor

    replay_stores = [SQLiteStore(env.database_path) for _ in range(2)]
    replay_publishers = [
        CandidatePublisher(
            replay_store,
            replay_store,
            env.genesis,
            env.recovery_key,
            env.recovery_signer,
        )
        for replay_store in replay_stores
    ]
    revision_before_replay = env.store.get_runtime_revision()
    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_results = list(
            executor.map(
                lambda pair: process_worker_failures(
                    pair[0],
                    candidate_publishers=CandidatePublisherRegistry(
                        (("recovery.publish.v1", pair[1]),)
                    ),
                ),
                zip(replay_stores, replay_publishers),
            )
        )
    for replay_store in replay_stores:
        replay_store.close()
    assert all(result in ([], [env.source.id]) for result in replay_results)
    assert env.store.get_runtime_revision() == revision_before_replay

    _recovery_one_claim, recovery_one_failure = _fail_once(
        env, recovery_one, "claim:tool-recovery-one"
    )
    recovery_two = next(
        item
        for item in env.store.get_all_contracts()
        if item.parent_id == env.consumer.id
        and item.id != recovery_one.id
        and item.outputs == env.source.outputs
    )
    assert recovery_two.budget == 1
    assert (
        allowance_available(
            env.consumer, [record for _, record in env.store.scan_runtime_records()]
        )
        == 1
    )
    assert _recovery_receipt_proves(
        env, recovery_two, env.source, recovery_one.id, recovery_one_failure.id
    )

    _recovery_two_claim, recovery_two_failure = _fail_once(
        env, recovery_two, "claim:tool-recovery-two"
    )
    before_unfunded = env.store.get_runtime_revision()
    process_worker_failures(env.store, candidate_publishers=env.publishers)
    after_maintenance = env.store.get_runtime_revision()
    assert after_maintenance == before_unfunded
    assert not any(
        item.parent_id == env.consumer.id
        and item.id not in {recovery_one.id, recovery_two.id}
        and item.outputs == env.source.outputs
        for item in env.store.get_all_contracts()
    )
    assert (
        allowance_available(
            env.consumer, [record for _, record in env.store.scan_runtime_records()]
        )
        == 1
    )

    snapshot_path = env.database_path
    digest_before = env.store.runtime_materialization_digest()
    env.store.close()
    reopened = SQLiteStore(snapshot_path)
    assert reopened.rebuild_runtime_materializations(strict=True) == digest_before
    assert (
        allowance_available(
            reopened.get_contract(env.consumer.id),
            [record for _, record in reopened.scan_runtime_records()],
        )
        == 1
    )
    assert reopened.get_contract(recovery_one.id) is not None
    assert reopened.get_contract(recovery_two.id) is not None
    assert reopened.get_runtime_revision() == after_maintenance
    reopened.close()


def _recovery_receipt_proves(env, recovery, source, failed_id, failure_id):
    rows = [record for _, record in env.store.scan_runtime_records()]
    declaration = next(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and row.payload["contract"]["id"] == recovery.id
    )
    receipt = next(
        row
        for row in rows
        if row.record_type == "candidate.received"
        and row.payload.get("candidate_id") == declaration.payload.get("candidate_id")
    )
    head = next(
        row
        for row in rows
        if row.record_type == "candidate.committed"
        and row.payload.get("candidate_id") == receipt.payload.get("candidate_id")
    )
    assert failed_id in receipt.causal_parents
    assert failure_id in receipt.causal_parents
    assert source.id in receipt.causal_parents
    assert recovery.parent_id == env.consumer.id
    assert recovery.budget == 1
    member_ids = tuple(head.payload["committed_record_ids"])
    members = [env.store.get_runtime_record(record_id) for record_id in member_ids]
    assert declaration.id in member_ids
    assert any(
        member is not None
        and member.record_type == "asset.committed"
        and member.payload["asset"]["name"] == f"_fail_context_{failed_id}.recovery"
        for member in members
    )
    grant = next(
        row
        for row in rows
        if row.record_type == "allowance.granted"
        and row.payload.get("contract_id") == recovery.id
    )
    reservation = next(
        row
        for row in rows
        if row.record_type == "allowance.reserved"
        and row.payload.get("child_contract_id") == recovery.id
    )
    assert grant.payload["source_contract_id"] == env.consumer.id
    assert reservation.payload["source_contract_id"] == env.consumer.id
    assert grant.id in member_ids and reservation.id in member_ids
    assert recovery.outputs == source.outputs
    return True
