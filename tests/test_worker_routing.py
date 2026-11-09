"""Capability routing tests: scheduler metadata never becomes prompt behavior."""

from __future__ import annotations

import sqlite3
import time

import pytest
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.llm import LLMWorker, ProviderError
from aigineering.agent.prompt import contract_prompt
from aigineering.cli.task_state import project_task_status
from aigineering.core.task_productivity import project_task_productivity
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_expired_claims,
    process_rejected_submissions,
    process_worker_failures,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.trace import create_entry
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.store import MemoryStore
from aigineering.core.worker_routing import (
    WorkerRegistration,
    eligible_workers,
    select_worker,
)
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Candidate, Contract
from aigineering.protocol.wire import trace_entry_to_dict


def _contract(**overrides) -> Contract:
    values = {
        "id": "task:route",
        "name": "review",
        "outputs": ("report",),
        "budget": 1,
        "worker_capabilities": ("vision", "strict-action"),
        "worker_pools": ("advanced",),
    }
    values.update(overrides)
    return Contract(**values)


def _signed_contract(store, contract: Contract):
    setup = candidate_runtime(store)
    return setup, setup.accept_contract(contract)


def _host_for(setup, store, worker):
    return hosted_worker(
        store,
        worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )


def _recovery_environment(store, suffix: str):
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        f"human:recovery-test-{suffix}",
        f"setup-{suffix}",
        setup_signer.kind,
        setup_signer.signer_id,
        ("actor.authorize", "worker.register", "contract.publish"),
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.v1",
        f"recovery-{suffix}",
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
        f"recovery-{suffix}", (setup_key, recovery_key), f"policy:recovery-{suffix}"
    )
    initialize_genesis(store, genesis)
    setup = candidate_runtime(
        store, genesis=genesis, actor_key=setup_key, signer=setup_signer
    )
    publishers = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(
                    store, store, genesis, recovery_key, recovery_signer
                ),
            ),
        )
    )
    return setup, publishers


def test_eligibility_requires_all_capabilities_and_one_permitted_pool():
    contract = _contract()
    workers = [
        WorkerRegistration(
            "text", capabilities=("strict-action",), pools=("advanced",)
        ),
        WorkerRegistration(
            "wrong-pool", capabilities=("vision", "strict-action"), pools=("default",)
        ),
        WorkerRegistration(
            "advanced", capabilities=("vision", "strict-action"), pools=("advanced",)
        ),
    ]

    assert [worker.worker_id for worker in eligible_workers(contract, workers)] == [
        "advanced"
    ]


def test_exclusive_tool_worker_cannot_claim_unqualified_reasoning_task():
    contract = Contract(id="task:generic", outputs=("report",))
    workers = (
        WorkerRegistration("llm"),
        WorkerRegistration("tools", capabilities=("tool-execution", "tool:lookup")),
    )

    assert [worker.worker_id for worker in eligible_workers(contract, workers)] == [
        "llm"
    ]
    tool_contract = Contract(
        id="task:tool",
        worker_capabilities=("tool-execution", "tool:lookup"),
    )
    assert [
        worker.worker_id for worker in eligible_workers(tool_contract, workers)
    ] == ["tools"]


def test_selection_is_least_loaded_then_worker_id():
    contract = _contract(worker_capabilities=(), worker_pools=())
    workers = [
        WorkerRegistration("z", active_claims=0),
        WorkerRegistration("a", active_claims=0),
        WorkerRegistration("busy", capacity=2, active_claims=1),
    ]

    assert select_worker(contract, workers).worker_id == "a"


def test_routing_constraints_do_not_become_prompt_context():
    contract = _contract(labels=("behavior:concise",))
    prompt = contract_prompt(contract, [])

    assert "worker_capabilities" not in prompt
    assert "advanced" not in prompt
    assert "vision" not in prompt


def test_sqlite_claim_requires_matching_registered_worker_and_records_profile():
    store = SQLiteStore()
    contract = _contract()
    store.add_contract(contract)
    store.register_worker(
        WorkerRegistration(
            "llm:text",
            capabilities=("strict-action",),
            pools=("advanced",),
            profile_id="openai-chat-v1",
        )
    )
    store.register_worker(
        WorkerRegistration(
            "llm:vision",
            capabilities=("vision", "strict-action"),
            pools=("advanced",),
            profile_id="deepseek-vision-v1",
        )
    )

    assert claim_next_package(store, worker_id="llm:text") is None
    claimed = claim_next_package(store, worker_id="llm:vision")

    assert claimed is not None
    assert claimed.package.capability_requirements == ("vision", "strict-action")
    assert claimed.package.worker_profile_id == "deepseek-vision-v1"
    assert claimed.package.worker_registration_version == "1"
    assert store.get_by_contract(contract.id)[-1].event_type == "worker_routed"


def test_missing_capability_is_visible_in_task_projection():
    store = SQLiteStore()
    contract = _contract()
    store.add_contract(contract)
    store.register_worker(WorkerRegistration("llm:text", capabilities=("text",)))

    status = project_task_status(contract, store)

    assert status["status"] == "blocked_capability"
    assert status["silent_failure_risks"] == [
        {
            "code": "blocked_capability",
            "message": "no registered worker currently satisfies routing constraints",
        }
    ]


def test_invalid_descendant_activation_is_visible_from_root_projection():
    store = SQLiteStore(":memory:")
    root = Contract(id="task:invalid-root", outputs=("report",), budget=2)
    child = Contract(
        id="task:invalid-child",
        parent_id=root.id,
        outputs=("evidence",),
        activation="data_file&citation_db",
        budget=1,
    )
    store.add_contract(root)
    store.add_contract(child)

    status = project_task_status(root, store)

    assert any(
        risk["code"] == "descendant_invalid_activation"
        for risk in status["silent_failure_risks"]
    )
    store.close()


def test_expanded_parent_does_not_report_budget_exhaustion_while_child_runs():
    store = SQLiteStore(":memory:")
    root = Contract(id="task:expanded-root", outputs=("report",), budget=0)
    child = Contract(
        id="task:running-child",
        parent_id=root.id,
        outputs=("report",),
        budget=1,
    )
    store.add_contract(root)
    store.add_contract(child)
    store.append_runtime_record(
        create_runtime_record(
            "attempt.closed",
            {
                "candidate_id": "candidate:expanded",
                "claim_id": "claim:expanded",
                "contract_id": root.id,
                "outcome": "expanded",
            },
        )
    )

    status = project_task_status(root, store)

    assert status["status"] == "expanded"
    assert "budget_exhausted" not in {
        risk["code"] for risk in status["silent_failure_risks"]
    }
    store.close()


def test_worker_execution_rejects_store_without_transactional_port():
    store = MemoryStore()
    store.add_contract(Contract(id="task:weak-store", outputs=("out",)))

    with pytest.raises(TypeError, match="transactional worker StorePort"):
        claim_next_package(store, worker_id="worker")


def test_missing_declared_input_prevents_claim_without_explicit_activation():
    store = SQLiteStore(":memory:")
    contract = Contract(
        id="task:missing-input",
        inputs=("evidence",),
        outputs=("report",),
        budget=1,
    )
    store.add_contract(contract)

    assert claim_next_package(store, worker_id="worker") is None
    store.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_worker_registration_versions_are_immutable_and_rebuildable(kind):
    store = MemoryStore() if kind == "memory" else SQLiteStore(":memory:")
    v1 = WorkerRegistration("worker", capabilities=("text",), version="1")
    store.register_worker(v1)
    store.register_worker(v1)

    with pytest.raises(ImmutableRecordConflict, match="registration version"):
        store.register_worker(
            WorkerRegistration("worker", capabilities=("vision",), version="1")
        )

    v2 = WorkerRegistration("worker", capabilities=("vision",), version="2")
    store.register_worker(v2)
    assert store.get_worker_registration("worker") == v2
    assert len(store.scan_runtime_records(record_type="worker.registered")) == 2

    if kind == "memory":
        store._worker_registrations.clear()
    else:
        with store._conn:
            store._conn.execute("DELETE FROM worker_registrations")
    store.rebuild_worker_registration_projection()
    assert store.get_worker_registration("worker") == v2
    if kind == "sqlite":
        store.close()


def test_claim_rechecks_registration_version_inside_transaction():
    store = SQLiteStore(":memory:")
    store.add_contract(Contract(id="task:versioned", outputs=("out",), budget=1))
    store.register_worker(WorkerRegistration("worker", version="2"))

    stale = store.claim_contract(
        "task:versioned", "worker", expected_registration_version="1"
    )
    current = store.claim_contract(
        "task:versioned", "worker", expected_registration_version="2"
    )

    assert stale is None
    assert current is not None
    store.close()


def test_long_worker_invocation_renews_claim_before_submit():
    class SlowWorker:
        worker_id = "slow-worker"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            time.sleep(1.1)
            return Candidate(worker_id=self.worker_id, raw_output="report: done")

    store = SQLiteStore(":memory:")
    setup, contract = _signed_contract(
        store, Contract(id="task:slow", outputs=("report",), budget=1)
    )
    host = _host_for(setup, store, SlowWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id, lease_seconds=1)
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "accepted"
    record_types = [record.record_type for _, record in store.scan_runtime_records()]
    assert "claim.renewed" in record_types
    assert store.get_claim(contract.id)["status"] == "submitted"
    store.close()


def test_renewal_failure_discards_worker_result(monkeypatch):
    class SlowWorker:
        worker_id = "slow-worker"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            time.sleep(0.5)
            return Candidate(worker_id=self.worker_id, raw_output="report: forbidden")

    store = SQLiteStore(":memory:")
    setup, contract = _signed_contract(
        store, Contract(id="task:lost-lease", outputs=("report",), budget=1)
    )
    host = _host_for(setup, store, SlowWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id, lease_seconds=1)
    assert claimed is not None
    monkeypatch.setattr(store, "renew_claim", lambda *args, **kwargs: None)

    with pytest.raises(WorkerInvocationError, match="durably closed"):
        execute_claimed_package(claimed, host, store)

    assert store.get_assets_by_name("report") == []
    assert store.get_claim(contract.id)["status"] == "released"
    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[-1][1]
    assert failure.payload["category"] == "worker_error:claim_renewal_failed"
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    extinguished = store.scan_runtime_records(record_type="allowance.extinguished")
    assert len(extinguished) == 1
    assert extinguished[0][1].payload["contract_id"] == contract.id
    assert extinguished[0][1].payload["amount"] == contract.budget
    store.close()


def test_unexpected_worker_adapter_failure_durably_closes_claim():
    class BrokenWorker:
        worker_id = "broken-worker"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise RuntimeError("private adapter detail")

    store = SQLiteStore(":memory:")
    setup, contract = _signed_contract(
        store, Contract(id="task:broken-adapter", outputs=("report",), budget=1)
    )
    host = _host_for(setup, store, BrokenWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    with pytest.raises(WorkerInvocationError, match="worker invocation failed"):
        execute_claimed_package(claimed, host, store)

    assert store.get_claim(contract.id)["status"] == "released"
    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[-1][1]
    assert failure.payload["category"] == "worker_error:invocation_failure"
    assert "private adapter detail" not in str(failure.payload)
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    extinguished = store.scan_runtime_records(record_type="allowance.extinguished")
    assert len(extinguished) == 1
    assert extinguished[0][1].payload["amount"] == contract.budget
    store.close()


def test_rejected_submission_recovery_replays_after_crash_gap():
    store = SQLiteStore(":memory:")
    contract = Contract(id="task:rejected-replay", outputs=("report",), budget=1)
    store.add_contract(contract)
    candidate = create_runtime_record(
        "candidate.received",
        {
            "candidate_id": "candidate:rejected-replay",
            "contract_id": contract.id,
            "raw_output": "undeclared: rejected",
            "worker_id": "worker",
        },
    )
    projection = create_runtime_record(
        "projection.decided",
        {
            "candidate_id": "candidate:rejected-replay",
            "contract_id": contract.id,
            "rejections": ({"name": "undeclared", "reject_reason": "undeclared"},),
            "status": "rejected",
        },
        causal_parents=(candidate.id,),
    )
    store.append_runtime_record(candidate)
    store.append_runtime_record(projection)
    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:recovery.publish.v1",
        "recovery-1",
        signer.kind,
        signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(
        "recovery-replay", (actor,), "policy:recovery-replay"
    )
    initialize_genesis(store, genesis)
    publishers = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(store, store, genesis, actor, signer),
            ),
        )
    )

    assert process_rejected_submissions(store, candidate_publishers=publishers) == []
    assert process_rejected_submissions(store, candidate_publishers=publishers) == []
    recovery_receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == actor.actor_id
    ]
    assert recovery_receipts == []
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    store.close()


def test_rejected_projection_without_raw_candidate_fails_loudly():
    store = SQLiteStore(":memory:")
    contract = Contract(id="task:missing-candidate", outputs=("report",), budget=1)
    store.add_contract(contract)
    store.append_runtime_record(
        create_runtime_record(
            "projection.decided",
            {
                "candidate_id": "candidate:missing",
                "contract_id": contract.id,
                "rejections": (),
                "status": "rejected",
            },
        )
    )

    with pytest.raises(RuntimeError, match="no replayable raw Candidate evidence"):
        process_rejected_submissions(store)

    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    store.close()


@pytest.mark.parametrize(
    ("record_type", "payload", "processor", "message"),
    [
        (
            "claim.expired",
            {
                "claim_id": "claim:missing",
                "contract_id": "task:missing",
                "epoch": 1,
                "lease_until": "2020-01-01T00:00:00+00:00",
                "package_id": "package:missing",
                "worker_id": "worker:missing",
            },
            process_expired_claims,
            "claim expiration .* references missing Contract",
        ),
        (
            "worker.invocation_failed",
            {
                "category": "provider_error",
                "claim_id": "claim:missing",
                "contract_id": "task:missing",
                "package_id": "package:missing",
                "retryable": True,
                "status_code": 503,
                "worker_id": "worker:missing",
            },
            process_worker_failures,
            "worker failure .* references missing Contract",
        ),
    ],
)
def test_recovery_replayers_never_silently_skip_missing_contracts(
    record_type, payload, processor, message
):
    store = SQLiteStore(":memory:")
    store.append_runtime_record(create_runtime_record(record_type, payload))

    with pytest.raises(RuntimeError, match=message):
        processor(store)

    store.close()


def test_provider_failure_releases_claim_without_persisting_secret():
    class FailingWorker:
        worker_id = "worker"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise ProviderError(503, "provider-secret-must-not-be-persisted")

    store = SQLiteStore(":memory:")
    setup, publishers = _recovery_environment(store, "provider-failure")
    contract = setup.accept_contract(
        Contract(id="task:provider-failure", outputs=("report",), budget=1)
    )
    host = _host_for(setup, store, FailingWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    with pytest.raises(WorkerInvocationError, match="claim was released"):
        execute_claimed_package(
            claimed,
            host,
            store,
            candidate_publishers=publishers,
        )

    assert store.get_claim(contract.id)["status"] == "released"
    assert process_worker_failures(store, candidate_publishers=publishers) == []
    records = [record for _, record in store.scan_runtime_records()]
    record_types = [record.record_type for record in records]
    assert "worker.invocation_failed" in record_types
    assert "worker_failure.recovery_scheduled" not in record_types
    assert "claim.released" in record_types
    assert not any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("terminal") == "failed"
        for record in records
    )
    extinguishments = [
        record for record in records if record.record_type == "allowance.extinguished"
    ]
    assert len(extinguishments) == 1
    assert extinguishments[0].payload["amount"] == contract.budget
    persisted = repr(
        (
            [dict(record.payload) for record in records],
            [trace_entry_to_dict(entry) for entry in store.get_all()],
        )
    )
    assert "provider-secret-must-not-be-persisted" not in persisted
    assert not any(
        child.parent_id == contract.id for child in store.get_all_contracts()
    )
    store.close()


def test_provider_failure_with_exhausted_parent_does_not_mint_recovery():
    """A failed child cannot reuse its exhausted parent's reserved allowance."""

    class FailingWorker:
        worker_id = "worker:no-allowance"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise ProviderError(503, "unavailable")

    store = SQLiteStore(":memory:")
    setup, publishers = _recovery_environment(store, "no-allowance")
    parent = setup.accept_contract(
        Contract(id="task:recovery-parent", outputs=("parent",), budget=1)
    )
    child = setup.accept_contract(
        Contract(
            id="task:recovery-child",
            parent_id=parent.id,
            origin="plan",
            outputs=("report",),
            budget=1,
        )
    )
    host = _host_for(setup, store, FailingWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id, contract_id=child.id)
    assert claimed is not None

    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(
            claimed,
            host,
            store,
            candidate_publishers=publishers,
        )

    record_types = [record.record_type for _, record in store.scan_runtime_records()]
    assert "worker_failure.recovery_unavailable" not in record_types
    assert "worker_failure.recovery_scheduled" not in record_types
    assert (
        process_worker_failures(
            store,
            candidate_publishers=publishers,
        )
        == []
    )
    assert not any(
        contract.parent_id == child.id for contract in store.get_all_contracts()
    )
    extinguished = store.scan_runtime_records(record_type="allowance.extinguished")
    assert any(row.payload.get("contract_id") == child.id for _, row in extinguished)
    store.close()


def test_stale_provider_failure_cannot_release_a_different_claim():
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:fenced-provider-failure", outputs=("report",), budget=1)
    )
    idle_worker = type("IdleWorker", (), {"worker_id": "worker"})()
    host = _host_for(setup, store, idle_worker)
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None

    with pytest.raises(
        sqlite3.IntegrityError, match="active worker claim predicate failed"
    ):
        store.commit_worker_invocation_failure(
            trace_entry=create_entry(contract.id, "attempted_stale_failure"),
            runtime_records=(),
            claim_id=claimed.package.claim_id,
            worker_id="different-worker",
            package_id=claimed.package.package_id,
            claim_epoch=claimed.package.claim_epoch,
        )

    assert store.get_claim(contract.id)["status"] == "active"
    assert store.get_by_contract(contract.id)[-1].event_type == "worker_routed"
    store.close()


def test_malformed_provider_response_releases_claim_and_recovers():
    store = SQLiteStore(":memory:")
    setup, publishers = _recovery_environment(store, "malformed-response")
    contract = setup.accept_contract(
        Contract(id="task:malformed-response", outputs=("report",), budget=1)
    )
    claimed = claim_next_package(store, worker_id="llm:test")
    assert claimed is not None
    worker = LLMWorker(
        model="test",
        api_key="test-only",
        transport=lambda _url, _headers, _payload: {
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
        },
    )

    with pytest.raises(WorkerInvocationError, match="claim was released"):
        execute_claimed_package(
            claimed,
            worker,
            store,
            candidate_publishers=publishers,
        )

    assert store.get_claim(contract.id)["status"] == "released"
    failure = store.scan_runtime_records(record_type="worker.invocation_failed")
    assert failure[-1][1].payload["category"] == "worker_error:response_missing_choices"
    assert failure[-1][1].payload["usage_metadata"]["total_tokens"] == 14
    status = project_task_status(contract, store)
    assert status["token_usage"] == [
        {
            "model": "test",
            "provider": "api.openai.com",
            "worker_profile": "openai-compatible-v1",
            "prompt_tokens": 11,
            "completion_tokens": 3,
            "total_tokens": 14,
        }
    ]
    assert not any(
        child.parent_id == contract.id for child in store.get_all_contracts()
    )
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    extinguished = store.scan_runtime_records(record_type="allowance.extinguished")
    assert any(row.payload.get("contract_id") == contract.id for _, row in extinguished)
    store.close()


def test_failed_usage_survives_sqlite_reopen_and_runtime_rebuild(tmp_path):
    db_path = tmp_path / "usage-failure.db"
    store = SQLiteStore(str(db_path))
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:usage-reopen", outputs=("report",), budget=1)
    )
    worker = LLMWorker(
        model="test",
        api_key="test-only",
        transport=lambda _url, _headers, _payload: {
            "choices": [],
            "usage": {"total_tokens": 9},
        },
    )
    host = hosted_worker(
        store,
        worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(claimed, host, store)

    before_status = project_task_status(contract, store)
    before_audit = project_task_productivity(contract, store)
    assert before_status["token_usage"] == [
        {
            "model": "test",
            "provider": "api.openai.com",
            "worker_profile": "openai-compatible-v1",
            "total_tokens": 9,
        }
    ]
    assert before_audit["token_totals"]["total_tokens"] == 9
    assert len(before_audit["usage_records"]) == 1
    digest = store.runtime_materialization_digest()
    store.close()

    reopened = SQLiteStore(str(db_path))
    try:
        restored = reopened.get_contract(contract.id)
        assert restored is not None
        assert (
            project_task_status(restored, reopened)["token_usage"]
            == before_status["token_usage"]
        )
        assert (
            project_task_productivity(restored, reopened)["usage_records"]
            == before_audit["usage_records"]
        )
        assert reopened.rebuild_runtime_materializations() == digest
        rebuilt_status = project_task_status(restored, reopened)
        rebuilt_audit = project_task_productivity(restored, reopened)
        assert rebuilt_status["token_usage"] == before_status["token_usage"]
        assert len(rebuilt_audit["usage_records"]) == 1
        assert rebuilt_audit["token_totals"]["total_tokens"] == 9
    finally:
        reopened.close()


def test_worker_encoding_failure_preserves_repairable_output_and_budget():
    class StructurallyInvalidWorker:
        worker_id = "worker:invalid-structure"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/parallel_tool {"calls": [{"name": "lookup"}]}',
                metadata={
                    "model": "local",
                    "total_tokens": 19,
                    "prompt_tokens": True,
                    "completion_tokens": -1,
                    "api_key": "secret",
                    "provider": "\ud800",
                    "ignored": {"password": "secret"},
                },
            )

    store = SQLiteStore(":memory:")
    setup, publishers = _recovery_environment(store, "invalid-structure")
    contract = setup.accept_contract(
        Contract(
            id="task:invalid-structure",
            outputs=("report",),
            budget=3,
            tool_scope=("lookup",),
        )
    )
    host = _host_for(setup, store, StructurallyInvalidWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(
            claimed,
            host,
            store,
            candidate_publishers=publishers,
        )

    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[-1][1]
    assert failure.payload["raw_output"].startswith("/parallel_tool")
    assert "between 2 and 8" in failure.payload["diagnostic"]
    assert failure.payload["usage_metadata"]["total_tokens"] == 19
    assert failure.payload["usage_metadata"] == {
        "model": "local",
        "total_tokens": 19,
    }
    assert project_task_status(contract, store)["token_usage"][-1]["total_tokens"] == 19
    assert not any(
        child.parent_id == contract.id for child in store.get_all_contracts()
    )
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    assert len(store.scan_runtime_records(record_type="allowance.extinguished")) == 1
    store.close()


def test_expired_claim_extinguishes_grant_without_minting_recovery():
    store = SQLiteStore(":memory:")
    setup, publishers = _recovery_environment(store, "expired-claim")
    contract = setup.accept_contract(
        Contract(id="task:expired-claim", outputs=("report",), budget=1)
    )
    host = _host_for(setup, store, type("IdleWorker", (), {"worker_id": "worker"})())
    claimed = claim_next_package(store, worker_id=host.worker_id, lease_seconds=1)
    assert claimed is not None
    time.sleep(1.05)

    assert process_expired_claims(store, candidate_publishers=publishers) == []
    assert process_expired_claims(store, candidate_publishers=publishers) == []
    assert store.get_claim(contract.id)["status"] == "expired"
    assert (
        claim_next_package(
            store,
            worker_id="replacement",
            contract_id=contract.id,
            candidate_publishers=publishers,
        )
        is None
    )
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    assert not any(
        child.parent_id == contract.id for child in store.get_all_contracts()
    )
    extinguished = store.scan_runtime_records(record_type="allowance.extinguished")
    assert len(extinguished) == 1
    assert extinguished[0][1].payload["contract_id"] == contract.id
    assert extinguished[0][1].payload["amount"] == contract.budget
    store.rebuild_claim_projection()
    assert store.get_claim(contract.id)["status"] == "expired"
    store.close()
