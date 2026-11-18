"""A single tool action predeclares its ordinary continuation atomically."""

from __future__ import annotations

from types import SimpleNamespace

from conftest import hosted_worker
from conftest import candidate_runtime

from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.publication_history import (
    index_published_records,
    validate_published_member,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.types import Asset, Contract
from aigineering.runtime import claim_next_package, execute_claimed_package


def _tool_environment(tmp_path, name: str):
    store = SQLiteStore(tmp_path / f"{name}.sqlite3")
    setup = candidate_runtime(store)
    env = SimpleNamespace(
        store=store,
        setup=setup,
        genesis=setup.genesis,
        publishers=CandidatePublisherRegistry(()),
    )
    parent = env.setup.accept_contract(
        Contract(
            id=f"task:{name}:root",
            name=f"{name}-root",
            inputs=("source_input",),
            outputs=("final_report",),
            budget=5,
            tool_scope=("lookup", "other"),
            sensitive_input_policy={"mode": "allow", "roles": ["source_input"]},
        )
    )
    env.setup.accept_asset(
        Asset(
            id=f"asset:{name}:source",
            name="source_input",
            content="frozen source input",
            created_by=env.setup.actor_key.actor_id,
        )
    )
    env.setup.accept_asset(
        create_tool_descriptor("lookup", "fixture lookup", {"type": "object"})
    )
    return env, parent


def _claimed_tool_host(env, parent):
    worker = MockWorker(worker_id=f"worker:{parent.name}")
    worker.set_output(
        parent.name,
        '/tool {"name":"lookup","args":{"query":"x"}}',
    )
    host = hosted_worker(
        env.store,
        worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claim = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=parent.id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id=f"claim:{parent.name}", contract_id=parent.id
        ),
    )
    assert claim is not None
    return host, claim


def test_signed_tool_candidate_declares_method_and_continuation_together(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    env, parent = _tool_environment(tmp_path, "ordinary-tool-continuation")
    host, claim = _claimed_tool_host(env, parent)
    result = execute_claimed_package(
        claim, host, env.store, candidate_publishers=env.publishers
    )
    assert result["status"] == "task_delegated", result

    children = [
        item for item in env.store.get_all_contracts() if item.parent_id == parent.id
    ]
    method_task = next(
        item for item in children if method_payload(item).get("method") == "tool"
    )
    continuation = next(item for item in children if item.origin == "continuation")
    assert method_task.budget == 1
    assert continuation.inputs == (*parent.inputs, *method_task.outputs)
    assert continuation.activation == method_task.outputs[0]
    assert continuation.tool_scope == parent.tool_scope
    assert continuation.outputs == parent.outputs
    assert continuation.sensitive_input_policy == parent.sensitive_input_policy
    assert (
        allowance_available(
            parent, [record for _, record in env.store.scan_runtime_records()]
        )
        == 1
    )

    rows = [record for _, record in env.store.scan_runtime_records()]
    index = index_published_records(rows)
    members = [
        row
        for row in rows
        if row.record_type == "contract.declared"
        and row.payload["contract"]["id"] in {method_task.id, continuation.id}
    ]
    assert len(members) == 2
    proofs = [
        validate_published_member(
            rows,
            member,
            expected_candidate=member.payload["candidate_id"],
            record_index=index,
        )
        for member in members
    ]
    assert proofs[0][0].id == proofs[1][0].id
    assert proofs[0][1].id == proofs[1][1].id
    reservations = [
        row
        for row in rows
        if row.record_type == "allowance.reserved"
        and row.payload.get("child_contract_id") in {method_task.id, continuation.id}
    ]
    assert {row.payload["child_contract_id"] for row in reservations} == {
        method_task.id,
        continuation.id,
    }
    assert all(row.causal_parents == (proofs[0][0].id,) for row in reservations)
    env.store.close()


def test_tool_pair_commit_rolls_back_both_declarations_on_second_write_failure(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services
    import aigineering.runtime as runtime
    from aigineering.plugins.recovery import RecoveryDisposition

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_schedule_rejected_recovery",
        lambda *args, **kwargs: RecoveryDisposition(None, "fixture recovery disabled"),
    )
    env, parent = _tool_environment(tmp_path, "ordinary-tool-rollback")
    host, claim = _claimed_tool_host(env, parent)
    env.store._conn.execute(
        """CREATE TRIGGER reject_tool_continuation
        BEFORE INSERT ON runtime_records
        WHEN NEW.record_type = 'contract.declared'
          AND json_extract(NEW.payload_json, '$.contract.origin') = 'continuation'
        BEGIN SELECT RAISE(ABORT, 'fixture continuation write failure'); END"""
    )
    result = execute_claimed_package(
        claim, host, env.store, candidate_publishers=env.publishers
    )
    assert result["status"] != "task_delegated", result
    children = [
        item for item in env.store.get_all_contracts() if item.parent_id == parent.id
    ]
    assert children == []
    records_after = [record for _, record in env.store.scan_runtime_records()]
    assert not any(
        record.record_type == "allowance.reserved"
        and record.payload.get("source_contract_id") == parent.id
        for record in records_after
    )
    env.store.close()


def test_tool_continuation_budget_keeps_review_and_recovery_reserve():
    from aigineering.plugins.continuation import allocate_continuation_budget
    from aigineering.plugins.delegation import TaskDelegationPlugin
    from aigineering.protocol.actions import WorkerAction

    parent = Contract(
        id="task:tool-budget:root",
        origin="human",
        outputs=("report",),
        budget=5,
        tool_scope=("lookup",),
        acceptance_policy={"mode": "independent"},
    )
    action = WorkerAction(type="tool", payload={"name": "lookup", "args": {}})
    proposal = TaskDelegationPlugin().propose_claimed(parent, action, allowance=4)
    assert len(proposal.effects) == 2
    expected, reserve = allocate_continuation_budget(parent, 3, recovery_demand=1)
    assert expected == 1
    assert reserve == 2
