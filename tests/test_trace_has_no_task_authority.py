"""Trace events remain observability data and cannot control task state."""

from __future__ import annotations

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.task_productivity import project_task_productivity
from aigineering.core.trace_manager import TraceManager
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.effect_builders import contract_cancellation_effect
from aigineering.protocol.types import Asset
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.task_views import project_task_status


def _state_fingerprint(contract, store):
    view = RuntimeProjection(store, store).contract_view(contract)
    status = project_task_status(contract, store)
    productivity = project_task_productivity(contract, store)
    return (
        view.enabled,
        view.blockers,
        view.terminal,
        view.terminal_events,
        view.budget_remaining,
        view.projection_hash,
        status["status"],
        status["terminal"],
        status["budget_remaining"],
        status["blockers"],
        status["qualification_pending_outputs"],
        productivity["contract_statuses"],
        productivity["terminal_statuses"],
    )


def _add_adversarial_traces(store, contract_id: str, start: int):
    trace = TraceManager(store)
    for offset, event_type in enumerate(
        (
            "complete",
            "failed",
            "projection",
            "task_delegated",
            "method_scheduled",
            "method_continuation_scheduled",
            "budget_consumed",
        )
    ):
        trace.record(
            contract_id,
            event_type,
            sequence=start + offset,
            budget_remaining=0,
        )


def test_trace_cannot_change_readiness_terminal_budget_or_qualification(
    monkeypatch, tmp_path
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    store = SQLiteStore(tmp_path / "trace-authority.sqlite3")
    setup = candidate_runtime(store)
    rubric = setup.accept_asset(
        build_control_plane_asset(
            name="trace-test-rubric", content="Review the report against criteria."
        )
    )
    evidence = setup.accept_asset(
        build_control_plane_asset(
            name="trace-test-evidence", content="Frozen source evidence."
        )
    )
    policy = {
        "mode": "independent",
        "policy_version": "trace-authority-test-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["verify.human"],
        "rubric_asset_ids": [rubric.id],
        "evidence_asset_ids": [evidence.id],
    }
    contract = setup.accept_contract(
        build_control_plane_contract(
            name="trace-authority-task",
            outputs=("trace_report",),
            budget=5,
            acceptance_policy=policy,
        )
    )
    before = _state_fingerprint(contract, store)
    assert before[0] is True
    assert before[6] == "ready"
    assert before[4] == 5

    _add_adversarial_traces(store, contract.id, 100)
    after_trace = _state_fingerprint(contract, store)
    assert after_trace == before

    worker = MockWorker(worker_id="worker:trace-authority")
    worker.set_output(
        contract.name,
        '/exec {"outputs":{"trace_report":"Submitted report."}}',
    )
    host = hosted_worker(
        store,
        worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claim = claim_next_package(store, worker_id=host.worker_id, contract_id=contract.id)
    assert claim is not None
    result = execute_claimed_package(claim, host, store)
    assert result["status"] == "accepted", result
    submitted = project_task_status(contract, store)
    assert submitted["status"] == "submitted"
    assert submitted["qualification_pending_outputs"] == {
        "trace_report": next(
            asset.id
            for asset in store.get_assets_by_name("trace_report")
            if asset.created_by == contract.id
        )
    }
    submitted_fingerprint = _state_fingerprint(contract, store)
    _add_adversarial_traces(store, contract.id, 200)
    assert _state_fingerprint(contract, store) == submitted_fingerprint
    assert (
        RuntimeProjection(store, store).contract_view(contract).claim_status
        == "submitted"
    )

    matching = setup.accept_contract(
        build_control_plane_contract(
            name="trace-authority-matching-asset",
            outputs=("matching_report",),
            budget=2,
        )
    )
    setup.accept_asset(
        Asset(
            id="asset:trace-authority:matching",
            name="matching_report",
            content="A committed matching output.",
            origin="human",
        )
    )
    assert project_task_status(matching, store)["status"] == "completed"

    cancelled = setup.accept_contract(
        build_control_plane_contract(
            name="trace-authority-cancelled", outputs=("never_written",), budget=2
        )
    )
    cancellation_signer = Ed25519Signer()
    cancellation_key = ActorKey(
        "test:trace-cancellation",
        "trace-cancellation-key",
        cancellation_signer.kind,
        cancellation_signer.signer_id,
        ("contract.cancel",),
    )
    setup.authorize_actor(cancellation_key)
    cancellation_publisher = CandidatePublisher(
        store,
        store,
        setup.genesis,
        cancellation_key,
        cancellation_signer,
    )
    decision = cancellation_publisher.publish(
        (contract_cancellation_effect(cancelled.id, "signed test cancellation"),),
        idempotency_key="trace-authority-cancel-task",
    )
    assert decision.accepted
    assert project_task_status(cancelled, store)["status"] == "cancelled"
    store.close()
