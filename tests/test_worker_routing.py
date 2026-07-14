"""Capability routing tests: scheduler metadata never becomes prompt behavior."""

from __future__ import annotations

import time

import pytest

from aigineering.agent.prompt import contract_prompt
from aigineering.cli.task_state import project_task_status
from aigineering.cli.worker_runtime import (
    claim_next_package,
    execute_claimed_package,
    process_rejected_submissions,
)
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.submit import submit_candidate
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.store import MemoryStore
from aigineering.core.worker_routing import (
    WorkerRegistration,
    eligible_workers,
    select_worker,
)
from aigineering.protocol.types import Candidate, Contract
from aigineering.protocol.envelope import CandidateEnvelope


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

    assert status["status"] == "waiting_for_capability"
    assert status["silent_failure_risks"] == [
        {
            "code": "waiting_for_capability",
            "message": "no registered worker currently satisfies routing constraints",
        }
    ]


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
        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            time.sleep(1.1)
            return Candidate(worker_id="slow", raw_output="report: done")

    store = SQLiteStore(":memory:")
    contract = Contract(id="task:slow", outputs=("report",), budget=1)
    store.add_contract(contract)
    claimed = claim_next_package(store, worker_id="slow-worker", lease_seconds=1)
    assert claimed is not None

    result = execute_claimed_package(claimed, SlowWorker(), store)

    assert result["status"] == "accepted"
    record_types = [record.record_type for _, record in store.scan_runtime_records()]
    assert "claim.renewed" in record_types
    assert store.get_claim(contract.id)["status"] == "submitted"
    store.close()


def test_renewal_failure_discards_worker_result(monkeypatch):
    class SlowWorker:
        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            time.sleep(0.5)
            return Candidate(worker_id="slow", raw_output="report: forbidden")

    store = SQLiteStore(":memory:")
    contract = Contract(id="task:lost-lease", outputs=("report",), budget=1)
    store.add_contract(contract)
    claimed = claim_next_package(store, worker_id="slow-worker", lease_seconds=1)
    assert claimed is not None
    monkeypatch.setattr(store, "renew_claim", lambda *args, **kwargs: None)

    with pytest.raises(ValueError, match="result was not submitted"):
        execute_claimed_package(claimed, SlowWorker(), store)

    assert store.get_assets_by_name("report") == []
    assert store.get_claim(contract.id)["status"] == "active"
    store.close()


def test_rejected_submission_recovery_replays_after_crash_gap():
    store = SQLiteStore(":memory:")
    contract = Contract(id="task:rejected-replay", outputs=("report",), budget=1)
    store.add_contract(contract)
    claimed = claim_next_package(store, worker_id="worker")
    assert claimed is not None
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id=claimed.worker_id,
        raw_output="undeclared: rejected",
        package_id=claimed.package.package_id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
    )
    ingress = RuntimeIngress(store, store, FactReducer(store, store))

    result = submit_candidate(envelope, store, store, ingress)

    assert result["status"] == "rejected"
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")

    assert process_rejected_submissions(store) == [contract.id]
    assert process_rejected_submissions(store) == []
    terminal_records = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert terminal_records[-1][1].payload["terminal"] == "failed"
    contracts = store.get_all_contracts()
    assert any(
        child.name == f"{contract.name or contract.id}.recover"
        and child.origin == "recovery"
        for child in contracts
    ), contracts
    store.close()
