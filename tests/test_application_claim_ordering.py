from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerHost
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
from aigineering.plugins.task_assignment import TASK_ASSIGNMENT_EFFECTS
from aigineering.plugins.task_dependencies import TASK_DEPENDENCY_EFFECTS
from aigineering.plugins.task_priority import TASK_PRIORITY_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    candidate_proposal_to_dict,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    task_assign_effect,
    task_block_effect,
    task_dependency_link_effect,
    task_priority_effect,
    worker_claim_effect,
)
from aigineering.protocol.types import Contract
from aigineering.runtime_services import (
    claim_next_application_package,
    order_application_contract_ids,
)


def _world(path):
    store = SQLiteStore(path)
    admin_signer = Ed25519Signer()
    admin = ActorKey(
        "test:priority-order-admin",
        "priority-order-admin-key",
        admin_signer.kind,
        admin_signer.signer_id,
        (
            "actor.authorize",
            "worker.register",
            "contract.publish",
            "task.priority.manage",
            "task.block",
            "task.assignment.manage",
            "task.dependency.manage",
        ),
    )
    worker_signer = Ed25519Signer()
    worker_key = ActorKey(
        "worker:priority-order",
        "priority-order-worker-key",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    alternate_signer = Ed25519Signer()
    alternate_key = ActorKey(
        "worker:priority-other",
        "priority-other-key",
        alternate_signer.kind,
        alternate_signer.signer_id,
        ("worker.submit",),
    )
    genesis = create_genesis_manifest(
        "application-priority-order-test",
        (admin, worker_key, alternate_key),
        "priority-order",
    )
    initialize_genesis(store, genesis)
    store.register_worker(
        WorkerRegistration(
            worker_key.actor_id,
            version="1",
            actor_id=worker_key.actor_id,
            key_id=worker_key.key_id,
        )
    )
    store.register_worker(
        WorkerRegistration(
            alternate_key.actor_id,
            version="1",
            actor_id=alternate_key.actor_id,
            key_id=alternate_key.key_id,
        )
    )
    publisher = CandidatePublisher(
        store,
        store,
        genesis,
        admin,
        admin_signer,
        additional_effects={
            **TASK_PRIORITY_EFFECTS,
            **COLLABORATION_EFFECTS,
            **TASK_ASSIGNMENT_EFFECTS,
            **TASK_DEPENDENCY_EFFECTS,
        },
    )
    host = WorkerHost(
        MockWorker(worker_id=worker_key.actor_id),
        genesis,
        worker_key,
        worker_signer,
        registration_version="1",
    )
    return store, publisher, host


def _contract(name: str, *, activation: str = "") -> Contract:
    fields = {
        "name": name,
        "description": f"Work for {name}.",
        "inputs": (),
        "outputs": (f"{name}-result",),
        "activation": activation,
        "budget": 2,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _declare(publisher, contract):
    decision = publisher.publish(
        (contract_declaration_effect(contract),),
        idempotency_key=f"declare:{contract.name}",
    )
    assert decision.accepted, decision.runtime_records


def _set_priority(publisher, contract, priority):
    declarations = [
        record
        for _revision, record in publisher.store.scan_runtime_records(
            record_type="contract.declared"
        )
        if record.payload.get("contract", {}).get("id") == contract.id
    ]
    assert len(declarations) == 1
    decision = publisher.publish(
        (task_priority_effect(contract.id, priority, "", f"priority {priority}"),),
        idempotency_key=f"priority:{contract.name}",
        causal_parents=(declarations[0].id,),
    )
    assert decision.accepted


def test_application_claim_uses_priority_and_preserves_supplied_scope(tmp_path):
    store, publisher, host = _world(tmp_path / "application-priority.db")
    low = _contract("low")
    high = _contract("high")
    _declare(publisher, low)
    _declare(publisher, high)
    _set_priority(publisher, high, 20)

    scoped = order_application_contract_ids(store, (low.id, high.id))
    assert scoped == (high.id, low.id)
    assert order_application_contract_ids(store, (low.id,)) == (low.id,)

    package = claim_next_application_package(
        store,
        worker_id=host.worker_id,
        claim_candidate=host.claim_candidate(request_id="application-priority-scan"),
        expected_registration_version=host.registration_version,
    )
    assert package is not None and package.contract.id == high.id
    assert store.get_claim(high.id) is not None
    assert store.get_claim(low.id) is None
    store.close()


def test_high_priority_blocker_does_not_block_lower_ready_task(tmp_path):
    store, publisher, host = _world(tmp_path / "application-priority-blocked.db")
    blocked = _contract("blocked", activation="missing_input")
    ready = _contract("ready")
    _declare(publisher, blocked)
    _declare(publisher, ready)
    _set_priority(publisher, blocked, 100)

    package = claim_next_application_package(
        store,
        worker_id=host.worker_id,
        claim_candidate=host.claim_candidate(request_id="application-priority-blocked"),
        expected_registration_version=host.registration_version,
    )
    assert package is not None and package.contract.id == ready.id
    assert store.get_claim(blocked.id) is None
    store.close()


def test_high_priority_hold_does_not_block_lower_ready_task(tmp_path):
    store, publisher, host = _world(tmp_path / "application-priority-hold.db")
    blocked = _contract("held")
    ready = _contract("unheld")
    _declare(publisher, blocked)
    _declare(publisher, ready)
    _set_priority(publisher, blocked, 100)
    hold = publisher.publish(
        (task_block_effect(blocked.id, "pause this urgent task"),),
        idempotency_key="hold-urgent-task",
    )
    assert hold.accepted

    package = claim_next_application_package(
        store,
        worker_id=host.worker_id,
        claim_candidate=host.claim_candidate(request_id="application-priority-hold"),
        expected_registration_version=host.registration_version,
    )
    assert package is not None and package.contract.id == ready.id
    assert store.get_claim(blocked.id) is None
    store.close()


def test_high_priority_assigned_to_other_worker_does_not_displace_ready_task(
    tmp_path,
):
    store, publisher, host = _world(tmp_path / "application-priority-assigned.db")
    assigned = _contract("urgent-assigned")
    ready = _contract("ready-for-current-worker")
    _declare(publisher, assigned)
    _declare(publisher, ready)
    _set_priority(publisher, assigned, 100)
    transition = publisher.publish(
        (
            task_assign_effect(
                assigned.id,
                "worker:priority-other",
                "",
                "reserve urgent work for the specialist",
            ),
        ),
        idempotency_key="assign-urgent-to-other-worker",
    )
    assert transition.accepted

    package = claim_next_application_package(
        store,
        worker_id=host.worker_id,
        claim_candidate=host.claim_candidate(
            request_id="application-priority-assigned"
        ),
        expected_registration_version=host.registration_version,
    )
    assert package is not None and package.contract.id == ready.id
    assert store.get_claim(assigned.id) is None
    assert store.get_claim(ready.id) is not None
    assignments = [
        record
        for _revision, record in store.scan_runtime_records(
            record_type="claim.assignment.changed"
        )
        if record.payload.get("contract_id") == assigned.id
    ]
    assert len(assignments) == 1
    assert assignments[0].payload["worker_id"] == "worker:priority-other"
    store.close()


def test_high_priority_dependency_blocked_task_does_not_displace_ready_task(
    tmp_path,
):
    store, publisher, host = _world(tmp_path / "application-priority-dependency.db")
    prerequisite = _contract("unfinished-prerequisite")
    dependent = _contract("urgent-dependent")
    ready = _contract("independent-ready")
    for contract in (prerequisite, dependent, ready):
        _declare(publisher, contract)
    _set_priority(publisher, dependent, 100)
    _set_priority(publisher, prerequisite, -20)
    link = publisher.publish(
        (
            task_dependency_link_effect(
                dependent.id, prerequisite.id, "wait for the signed prerequisite"
            ),
        ),
        idempotency_key="link-urgent-prerequisite",
    )
    assert link.accepted

    package = claim_next_application_package(
        store,
        worker_id=host.worker_id,
        claim_candidate=host.claim_candidate(
            request_id="application-priority-dependency"
        ),
        expected_registration_version=host.registration_version,
    )
    assert package is not None and package.contract.id == ready.id
    assert store.get_claim(dependent.id) is None
    assert store.get_claim(ready.id) is not None
    assert any(
        record.payload.get("contract_id") == dependent.id
        and record.payload.get("prerequisite_contract_id") == prerequisite.id
        for _revision, record in store.scan_runtime_records(
            record_type="claim.constraint.placed"
        )
    )
    store.close()


def test_run_target_tree_claims_by_priority_without_expanding_scope(tmp_path):
    from types import SimpleNamespace

    from aigineering.cli.run import _claim_for_target_pool_host

    store, publisher, host = _world(tmp_path / "application-target-priority.db")
    target = _contract("target-root")
    child_fields = {
        "name": "target-child",
        "description": "A child in the selected target tree.",
        "inputs": (),
        "outputs": ("target-child-result",),
        "activation": "",
        "budget": 2,
        "tool_scope": (),
        "labels": (),
        "origin": "plan",
        "parent_id": target.id,
    }
    child = Contract(id=hash_contract_v3(**child_fields), **child_fields)
    unrelated = _contract("unrelated")
    for contract in (target, child, unrelated):
        _declare(publisher, contract)
    _set_priority(publisher, child, 30)
    pool_host = SimpleNamespace(host=host, required_contract_capability=None)

    claimed = _claim_for_target_pool_host(
        store, pool_host, None, target_task_id=target.id
    )
    assert claimed is not None and claimed.contract.id == child.id
    assert store.get_claim(child.id) is not None
    assert store.get_claim(target.id) is None
    assert store.get_claim(unrelated.id) is None
    store.close()


def test_quick_run_target_tree_uses_same_priority_order(tmp_path):
    from aigineering.cli._common import _claim_next_root_tree_package

    store, publisher, host = _world(tmp_path / "quick-target-priority.db")
    target = _contract("quick-root")
    child_fields = {
        "name": "quick-child",
        "description": "A child in the quick-run target tree.",
        "inputs": (),
        "outputs": ("quick-child-result",),
        "activation": "",
        "budget": 2,
        "tool_scope": (),
        "labels": (),
        "origin": "plan",
        "parent_id": target.id,
    }
    child = Contract(id=hash_contract_v3(**child_fields), **child_fields)
    outside = _contract("outside-quick-root")
    for contract in (target, child, outside):
        _declare(publisher, contract)
    _set_priority(publisher, child, 40)

    package = _claim_next_root_tree_package(
        store,
        worker_host=host,
        root_contract_id=target.id,
        candidate_publishers=None,
    )
    assert package is not None and package.contract.id == child.id
    assert store.get_claim(outside.id) is None
    store.close()


def test_fleet_slot_claims_by_priority_within_target_tree(tmp_path, monkeypatch):
    from queue import Queue
    from threading import Event

    import aigineering.local_fleet as fleet

    database = tmp_path / "fleet-priority.db"
    store, publisher, host = _world(database)
    target = _contract("fleet-root")
    child_fields = {
        "name": "fleet-child",
        "description": "A child in the fleet target tree.",
        "inputs": (),
        "outputs": ("fleet-child-result",),
        "activation": "",
        "budget": 2,
        "tool_scope": (),
        "labels": (),
        "origin": "plan",
        "parent_id": target.id,
    }
    child = Contract(id=hash_contract_v3(**child_fields), **child_fields)
    for contract in (target, child):
        _declare(publisher, contract)
    _set_priority(publisher, child, 60)
    store.close()

    executed = []
    stop = Event()
    monkeypatch.setattr(fleet, "require_redis", lambda: None)
    monkeypatch.setattr(fleet, "ensure_local_runtime_publishers", lambda _store: None)
    monkeypatch.setattr(fleet, "sync_runtime", lambda *_args, **_kwargs: None)

    def execute(claimed, *_args, **_kwargs):
        executed.append(claimed.contract.id)
        stop.set()
        return {"status": "accepted"}

    monkeypatch.setattr(fleet, "execute_captured_package", execute)
    fleet._run_slot(str(database), host, stop, Queue(), 0.01, target.id, [], Queue())

    assert executed == [child.id]
    result_store = SQLiteStore(database)
    assert result_store.get_claim(child.id) is not None
    assert result_store.get_claim(target.id) is None
    result_store.close()


def test_engine_worker_selector_enumerates_by_priority(tmp_path):
    from aigineering.agent.engine_worker import _claim_inner_work
    from aigineering.core.candidate_publisher import CandidatePublisherRegistry

    store, publisher, host = _world(tmp_path / "engine-priority.db")
    low = _contract("engine-low")
    high = _contract("engine-high")
    _declare(publisher, low)
    _declare(publisher, high)
    _set_priority(publisher, high, 80)

    selected = _claim_inner_work(
        store,
        host.worker,
        lambda _contract: host.worker,
        publisher,
        publisher.genesis,
        publisher.signer,
        {host.worker_id: (host.actor_key, host.signer)},
        CandidatePublisherRegistry(),
    )
    assert selected is not None and selected[0].contract.id == high.id
    assert store.get_claim(high.id) is not None
    assert store.get_claim(low.id) is None
    store.close()


def test_http_signed_broad_claim_uses_application_priority_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from aigineering.server.app import app

    monkeypatch.setattr("aigineering.server.app.sync_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr("aigineering.server.app.require_redis", lambda: None)
    (tmp_path / ".aig").mkdir()
    store = SQLiteStore(tmp_path / ".aig" / "store.db")
    signer = Ed25519Signer()
    key = ActorKey(
        "test:http-priority-worker",
        "http-priority-worker-key",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "worker.register",
            "contract.publish",
            "worker.submit",
            "task.priority.manage",
        ),
    )
    genesis = create_genesis_manifest("http-priority-test", (key,), "priority")
    initialize_genesis(store, genesis)
    store.register_worker(
        WorkerRegistration(
            key.actor_id,
            version="1",
            actor_id=key.actor_id,
            key_id=key.key_id,
        )
    )
    publisher = CandidatePublisher(
        store,
        store,
        genesis,
        key,
        signer,
        additional_effects=TASK_PRIORITY_EFFECTS,
    )
    low = _contract("http-low")
    high = _contract("http-high")
    _declare(publisher, low)
    _declare(publisher, high)
    _set_priority(publisher, high, 50)
    store.close()

    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(worker_claim_effect(key.actor_id, contract_id=None),),
        signer=signer,
        idempotency_key="http-priority-broad-claim",
    )
    response = TestClient(app).post(
        "/worker/claims", json=candidate_proposal_to_dict(candidate)
    )
    assert response.status_code == 200, response.text
    assert response.json()["contract_id"] == high.id
    claim_store = SQLiteStore(tmp_path / ".aig" / "store.db")
    assert claim_store.get_claim(high.id) is not None
    assert claim_store.get_claim(low.id) is None
    claim_store.close()
