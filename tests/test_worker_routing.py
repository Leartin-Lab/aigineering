"""Capability routing tests: scheduler metadata never becomes prompt behavior."""

from __future__ import annotations

import pytest

from aigineering.agent.prompt import contract_prompt
from aigineering.cli.task_state import project_task_status
from aigineering.cli.worker_runtime import claim_next_package
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.store import MemoryStore
from aigineering.core.worker_routing import (
    WorkerRegistration,
    eligible_workers,
    select_worker,
)
from aigineering.protocol.types import Contract


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
