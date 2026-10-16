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


def _recovery_publishers(store, suffix: str) -> CandidatePublisherRegistry:
    signer = Ed25519Signer()
    actor = ActorKey(
        f"plugin:recovery.publish.{suffix}",
        f"recovery-{suffix}",
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
        f"recovery-{suffix}", (actor,), f"policy:recovery-{suffix}"
    )
    initialize_genesis(store, genesis)
    return CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(store, store, genesis, actor, signer),
            ),
        )
    )


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
    contract = Contract(id="task:slow", outputs=("report",), budget=1)
    store.add_contract(contract)
    host = hosted_worker(store, SlowWorker())
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
    contract = Contract(id="task:lost-lease", outputs=("report",), budget=1)
    store.add_contract(contract)
    host = hosted_worker(store, SlowWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id, lease_seconds=1)
    assert claimed is not None
    monkeypatch.setattr(store, "renew_claim", lambda *args, **kwargs: None)

    with pytest.raises(WorkerInvocationError, match="durably closed"):
        execute_claimed_package(claimed, host, store)

    assert store.get_assets_by_name("report") == []
    assert store.get_claim(contract.id)["status"] == "released"
    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[-1][1]
    assert failure.payload["category"] == "worker_error:claim_renewal_failed"
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")[-1][1]
    assert terminal.payload == {"contract_id": contract.id, "terminal": "failed"}
    store.close()


def test_unexpected_worker_adapter_failure_durably_closes_claim():
    class BrokenWorker:
        worker_id = "broken-worker"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise RuntimeError("private adapter detail")

    store = SQLiteStore(":memory:")
    contract = Contract(id="task:broken-adapter", outputs=("report",), budget=1)
    store.add_contract(contract)
    host = hosted_worker(store, BrokenWorker())
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None

    with pytest.raises(WorkerInvocationError, match="worker invocation failed"):
        execute_claimed_package(claimed, host, store)

    assert store.get_claim(contract.id)["status"] == "released"
    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[-1][1]
    assert failure.payload["category"] == "worker_error:invocation_failure"
    assert "private adapter detail" not in str(failure.payload)
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")[-1][1]
    assert terminal.payload["terminal"] == "failed"
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
    store.append_runtime_record(
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": contract.id, "terminal": "failed"},
            causal_parents=(projection.id,),
        )
    )

    terminal_records = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert terminal_records[-1][1].payload["terminal"] == "failed"

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

    assert process_rejected_submissions(store, candidate_publishers=publishers) == [
        contract.id
    ]
    assert process_rejected_submissions(store, candidate_publishers=publishers) == []
    terminal_records = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert terminal_records[-1][1].payload["terminal"] == "failed"
    contracts = store.get_all_contracts()
    assert any(
        child.name == f"{contract.name or contract.id}.recover"
        and child.origin == "recovery"
        for child in contracts
    ), contracts
    recovery_receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == actor.actor_id
    ]
    assert len(recovery_receipts) == 1
    store.close()


def test_recovery_builder_recognizes_exact_accepted_publication():
    from aigineering.core.trace_manager import TraceManager
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.plugins.recovery import schedule_projection_recovery

    store = SQLiteStore(":memory:")
    failed = Contract(
        id="task:exact-recovery-replay",
        name="exact-recovery-replay",
        outputs=("report",),
        budget=1,
    )
    store.add_contract(failed)
    publishers = _recovery_publishers(store, "exact-recovery-replay")
    context = TaskCompletionContext(store, TraceManager(store), publishers)
    args = {
        "runtime": context,
        "failed_contract": failed,
        "candidate_raw": "malformed output",
        "rejections": [
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    }

    first = schedule_projection_recovery(**args)
    second = schedule_projection_recovery(**args)

    assert first is not None
    assert second == first, [
        (entry.event_type, entry.rejected_fragments) for entry in store.get_all()
    ]
    assert (
        len(
            [
                record
                for _, record in store.scan_runtime_records(
                    record_type="candidate.received"
                )
                if str(record.payload.get("actor_id", "")).startswith(
                    "plugin:recovery.publish."
                )
            ]
        )
        == 1
    )
    store.close()


def test_same_id_unpublished_recovery_contract_is_not_recognized_as_success():
    from aigineering.core.candidate_publisher import (
        CandidatePublisher,
        CandidatePublisherRegistry,
    )
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.signing import Ed25519Signer
    from aigineering.core.trace_manager import TraceManager
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.plugins.recovery import schedule_projection_recovery
    from aigineering.protocol.candidate import ActorKey, create_genesis_manifest

    failed = Contract(
        id="task:unpublished-recovery",
        name="unpublished-recovery",
        outputs=("report",),
        budget=1,
    )
    seed_store = SQLiteStore(":memory:")
    seed_store.add_contract(failed)
    seed_signer = Ed25519Signer()
    seed_actor = ActorKey(
        "plugin:recovery.publish.v1",
        "seed-recovery",
        seed_signer.kind,
        seed_signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    seed_genesis = create_genesis_manifest(
        "seed-recovery", (seed_actor,), "policy:seed"
    )
    initialize_genesis(seed_store, seed_genesis)
    seed_publishers = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(
                    seed_store, seed_store, seed_genesis, seed_actor, seed_signer
                ),
            ),
        )
    )
    seed_context = TaskCompletionContext(
        seed_store, TraceManager(seed_store), seed_publishers
    )
    existing = schedule_projection_recovery(
        seed_context,
        failed_contract=failed,
        candidate_raw="malformed output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    )
    assert existing is not None
    seed_store.close()

    store = SQLiteStore(":memory:")
    store.add_contract(failed)
    store.add_contract(existing)
    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:recovery.publish.v1",
        "unpublished-recovery",
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
        "unpublished-recovery", (actor,), "policy:unpublished"
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
    context = TaskCompletionContext(store, TraceManager(store), publishers)

    result = schedule_projection_recovery(
        context,
        failed_contract=failed,
        candidate_raw="malformed output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    )

    assert result is None
    assert not any(
        record.record_type == "candidate.received"
        for _, record in store.scan_runtime_records()
    )
    store.close()


def test_existing_child_recovery_is_recognized_after_parent_allowance_is_spent():
    from aigineering.core.trace_manager import TraceManager
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.plugins.recovery import schedule_projection_recovery
    from aigineering.protocol.candidate import ActorKey, create_genesis_manifest

    store = SQLiteStore(":memory:")
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        "test:recovery-allowance",
        "setup",
        setup_signer.kind,
        setup_signer.signer_id,
        ("contract.publish",),
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.allowance",
        "recovery",
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
        "recovery-allowance", (setup_key, recovery_key), "policy:recovery-allowance"
    )
    initialize_genesis(store, genesis)
    setup = candidate_runtime(
        store, genesis=genesis, actor_key=setup_key, signer=setup_signer
    )
    root = setup.accept_contract(
        Contract(id="task:allowance-root", name="allowance-root", budget=3)
    )
    failed = setup.accept_contract(
        Contract(
            id="task:allowance-child",
            name="allowance-child",
            parent_id=root.id,
            outputs=("report",),
            budget=1,
        )
    )
    recovery_publishers = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(
                    store, store, genesis, recovery_key, recovery_signer
                ),
            ),
        )
    )
    context = TaskCompletionContext(store, TraceManager(store), recovery_publishers)
    args = {
        "runtime": context,
        "failed_contract": failed,
        "candidate_raw": "malformed child output",
        "rejections": [
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    }
    first = schedule_projection_recovery(**args)
    assert first is not None
    setup.accept_contract(
        Contract(
            id="task:allowance-sibling",
            name="allowance-sibling",
            parent_id=root.id,
            outputs=("sibling",),
            budget=1,
        )
    )
    receipts_before = len(
        [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("actor_id") == recovery_key.actor_id
        ]
    )
    allowance_records_before = [
        record.record_type
        for _, record in store.scan_runtime_records()
        if record.record_type in {"allowance.granted", "allowance.reserved"}
    ]

    second = schedule_projection_recovery(**args)

    assert second == first
    assert (
        len(
            [
                record
                for _, record in store.scan_runtime_records(
                    record_type="candidate.received"
                )
                if record.payload.get("actor_id") == recovery_key.actor_id
            ]
        )
        == receipts_before
    )
    assert [
        record.record_type
        for _, record in store.scan_runtime_records()
        if record.record_type in {"allowance.granted", "allowance.reserved"}
    ] == allowance_records_before
    store.close()


@pytest.mark.parametrize(
    ("revoke_after_publish", "read_failure", "unstable_capture"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_signed_worker_failure_reconciles_accepted_recovery_after_reopen(
    tmp_path, monkeypatch, revoke_after_publish, read_failure, unstable_capture
):
    from aigineering.agent.worker import WorkerExecutionError
    from aigineering.agent.worker import WorkerHost
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.worker_routing import WorkerRegistration
    from aigineering.protocol.effect_builders import (
        actor_authorization_effect,
        worker_registration_effect,
    )
    from aigineering.protocol.effect_builders import actor_revocation_effect
    from aigineering.runtime import execute_claimed_package

    class FailingWorker:
        worker_id = "worker:recovery-crash-gap"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("fixture_failure", "safe fixture failure")

    db_path = tmp_path / f"recovery-crash-gap-{revoke_after_publish}.db"
    store = SQLiteStore(str(db_path))
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        "test:recovery-crash-gap",
        "setup",
        setup_signer.kind,
        setup_signer.signer_id,
        (
            "actor.authorize",
            "actor.revoke",
            "contract.publish",
            "worker.register",
        ),
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.crash-gap",
        "recovery",
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
        "recovery-crash-gap", (setup_key, recovery_key), "policy:recovery-crash-gap"
    )
    initialize_genesis(store, genesis)
    setup = candidate_runtime(
        store, genesis=genesis, actor_key=setup_key, signer=setup_signer
    )
    failed = setup.accept_contract(
        Contract(
            id="task:signed-recovery-crash-gap",
            name="signed-recovery-crash-gap",
            outputs=("report",),
            budget=1,
        )
    )
    worker_signer = Ed25519Signer()
    worker_key = ActorKey(
        "worker:recovery-crash-gap",
        "worker-key",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    registration = WorkerRegistration(
        worker_key.actor_id,
        version="1",
        actor_id=worker_key.actor_id,
        key_id=worker_key.key_id,
    )
    registration_decision = setup.publisher.publish(
        (
            actor_authorization_effect(worker_key),
            worker_registration_effect(registration),
        ),
        idempotency_key="register:signed-recovery-crash-gap",
    )
    assert registration_decision.accepted
    host = WorkerHost(
        FailingWorker(),
        genesis,
        worker_key,
        worker_signer,
        registration_version="1",
    )
    claim = host.claim_candidate(
        request_id="claim:signed-recovery-crash-gap", contract_id=failed.id
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=failed.id,
        expected_registration_version=host.registration_version,
        claim_candidate=claim,
    )
    assert claimed is not None
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

    import aigineering.runtime as runtime_module

    def crash_before_outcome(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected crash before recovery outcome")

    monkeypatch.setattr(
        runtime_module, "_commit_recovery_outcome", crash_before_outcome
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        execute_claimed_package(claimed, host, store, candidate_publishers=publishers)
    monkeypatch.undo()
    recovery_contracts = [
        contract
        for contract in store.get_all_contracts()
        if contract.origin == "recovery"
    ]
    assert len(recovery_contracts) == 1
    assert store.get_assets_by_name(f"_fail_context_{failed.id}")
    assert any(
        record.record_type == "worker.invocation_failed"
        for _, record in store.scan_runtime_records()
    )
    assert not any(
        record.record_type
        in {
            "worker_failure.recovery_scheduled",
            "worker_failure.recovery_unavailable",
        }
        for _, record in store.scan_runtime_records()
    )
    if revoke_after_publish:
        revocation = setup.publisher.publish(
            (
                actor_revocation_effect(
                    recovery_key.actor_id, recovery_key.key_id, "fixture rotation"
                ),
            ),
            idempotency_key="revoke:recovery-crash-gap",
        )
        assert revocation.accepted
    receipt_count = len(
        [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("actor_id") == recovery_key.actor_id
        ]
    )
    store.close()

    reopened = SQLiteStore(str(db_path))
    reopened_publishers = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(
                    reopened, reopened, genesis, recovery_key, recovery_signer
                ),
            ),
        )
    )
    if read_failure:
        monkeypatch.setattr(
            reopened,
            "get_runtime_revision",
            lambda: (_ for _ in ()).throw(OSError("fixture Store read failure")),
        )
        with pytest.raises(OSError, match="fixture Store read failure"):
            process_worker_failures(reopened, candidate_publishers=reopened_publishers)
        assert not any(
            record.record_type
            in {
                "worker_failure.recovery_scheduled",
                "worker_failure.recovery_unavailable",
            }
            for _, record in reopened.scan_runtime_records()
        )
        monkeypatch.undo()
    if unstable_capture:
        revisions = iter((1, 2, 3, 4, 5, 6))
        monkeypatch.setattr(reopened, "get_runtime_revision", lambda: next(revisions))
        assert process_worker_failures(
            reopened, candidate_publishers=reopened_publishers
        ) == [failed.id]
        assert (
            len(
                reopened.scan_runtime_records(
                    record_type="worker_failure.recovery_unavailable"
                )
            )
            == 1
        )
        assert not reopened.scan_runtime_records(
            record_type="worker_failure.recovery_scheduled"
        )
        assert (
            process_worker_failures(reopened, candidate_publishers=reopened_publishers)
            == []
        )
        reopened.close()
        return
    assert process_worker_failures(
        reopened, candidate_publishers=reopened_publishers
    ) == [failed.id]
    assert (
        process_worker_failures(reopened, candidate_publishers=reopened_publishers)
        == []
    )
    assert (
        len(
            [
                record
                for _, record in reopened.scan_runtime_records(
                    record_type="candidate.received"
                )
                if record.payload.get("actor_id") == recovery_key.actor_id
            ]
        )
        == receipt_count
    )
    assert (
        len(
            [
                record
                for _, record in reopened.scan_runtime_records(
                    record_type="worker_failure.recovery_scheduled"
                )
            ]
        )
        == 1
    )
    assert not any(
        record.record_type == "candidate.authentication_rejected"
        for _, record in reopened.scan_runtime_records()
    )
    reopened.close()


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
        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise ProviderError(503, "provider-secret-must-not-be-persisted")

    store = SQLiteStore(":memory:")
    contract = Contract(id="task:provider-failure", outputs=("report",), budget=1)
    store.add_contract(contract)
    claimed = claim_next_package(store, worker_id="worker")
    assert claimed is not None
    publishers = _recovery_publishers(store, "provider-failure")

    with pytest.raises(WorkerInvocationError, match="claim was released"):
        execute_claimed_package(
            claimed,
            FailingWorker(),
            store,
            candidate_publishers=publishers,
        )

    assert store.get_claim(contract.id)["status"] == "released"
    assert process_worker_failures(store) == []
    records = [record for _, record in store.scan_runtime_records()]
    record_types = [record.record_type for record in records]
    assert "worker.invocation_failed" in record_types
    assert "worker_failure.recovery_scheduled" in record_types
    assert "claim.released" in record_types
    assert any(
        record.record_type == "lifecycle.terminal"
        and record.payload["terminal"] == "failed"
        for record in records
    )
    persisted = repr(
        (
            [dict(record.payload) for record in records],
            [trace_entry_to_dict(entry) for entry in store.get_all()],
        )
    )
    assert "provider-secret-must-not-be-persisted" not in persisted
    assert any(child.origin == "recovery" for child in store.get_all_contracts())
    store.close()


def test_provider_failure_records_when_recovery_has_no_causal_allowance():
    """A rejected recovery publication must not be reported as scheduled."""

    class FailingWorker:
        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise ProviderError(503, "unavailable")

    store = SQLiteStore(":memory:")
    parent = Contract(id="task:recovery-parent", outputs=("report",), budget=0)
    child = Contract(
        id="task:recovery-child",
        parent_id=parent.id,
        outputs=("report",),
        budget=1,
    )
    store.add_contract(parent)
    store.add_contract(child)
    claimed = claim_next_package(store, worker_id="worker", contract_id=child.id)
    assert claimed is not None
    publishers = _recovery_publishers(store, "no-allowance")

    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(
            claimed,
            FailingWorker(),
            store,
            candidate_publishers=publishers,
        )

    record_types = [record.record_type for _, record in store.scan_runtime_records()]
    assert "worker_failure.recovery_unavailable" in record_types
    assert "worker_failure.recovery_scheduled" not in record_types
    assert (
        process_worker_failures(
            store,
            candidate_publishers=publishers,
        )
        == []
    )
    assert not any(
        contract.origin == "recovery" for contract in store.get_all_contracts()
    )
    store.close()


def test_stale_provider_failure_cannot_release_a_different_claim():
    store = SQLiteStore(":memory:")
    contract = Contract(id="task:fenced-provider-failure", outputs=("report",))
    store.add_contract(contract)
    claimed = claim_next_package(store, worker_id="worker")
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
    contract = Contract(id="task:malformed-response", outputs=("report",), budget=1)
    store.add_contract(contract)
    claimed = claim_next_package(store, worker_id="llm:test")
    assert claimed is not None
    worker = LLMWorker(
        model="test",
        api_key="test-only",
        transport=lambda _url, _headers, _payload: {
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
        },
    )
    publishers = _recovery_publishers(store, "malformed-response")

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
    assert any(child.origin == "recovery" for child in store.get_all_contracts())
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
    authority_signer = Ed25519Signer()
    authority_key = ActorKey(
        "test:authority",
        "test-authority-invalid-structure",
        authority_signer.kind,
        authority_signer.signer_id,
        ("actor.authorize", "worker.register"),
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.v1",
        "recovery-invalid-structure",
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
        "invalid-structure",
        (authority_key, recovery_key),
        "policy:invalid-structure",
    )
    initialize_genesis(store, genesis)
    contract = Contract(
        id="task:invalid-structure",
        outputs=("report",),
        budget=3,
        tool_scope=("lookup",),
    )
    store.add_contract(contract)
    host = hosted_worker(
        store,
        StructurallyInvalidWorker(),
        genesis=genesis,
        authority_key=authority_key,
        authority_signer=authority_signer,
    )
    claimed = claim_next_package(store, worker_id=host.worker_id)
    assert claimed is not None
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
    recovery = next(
        child for child in store.get_all_contracts() if child.origin == "recovery"
    )
    assert recovery.budget == contract.budget
    assert recovery.tool_scope == contract.tool_scope
    context = store.get_assets_by_name(f"_fail_context_{contract.id}")[-1]
    assert "/parallel_tool" in context.content
    assert "between 2 and 8" in context.content
    store.close()


def test_expired_claim_becomes_terminal_and_schedules_new_recovery_contract():
    store = SQLiteStore(":memory:")
    contract = Contract(id="task:expired-claim", outputs=("report",), budget=1)
    store.add_contract(contract)
    claimed = claim_next_package(store, worker_id="worker", lease_seconds=1)
    assert claimed is not None
    publishers = _recovery_publishers(store, "expired-claim")
    time.sleep(1.05)

    assert process_expired_claims(store, candidate_publishers=publishers) == [
        contract.id
    ]
    assert process_expired_claims(store, candidate_publishers=publishers) == []
    assert store.get_claim(contract.id)["status"] == "expired"
    assert (
        claim_next_package(store, worker_id="replacement", contract_id=contract.id)
        is None
    )
    terminal = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload["contract_id"] == contract.id
    ]
    assert len(terminal) == 1
    assert terminal[0].payload["terminal"] == "failed"
    assert any(
        child.origin == "recovery" and child.name == f"{contract.id}.recover"
        for child in store.get_all_contracts()
    )
    store.rebuild_claim_projection()
    assert store.get_claim(contract.id)["status"] == "expired"
    store.close()
