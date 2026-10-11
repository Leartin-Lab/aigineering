from __future__ import annotations

import json
import sqlite3
from threading import Event, Thread

import pytest

from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.runtime import claim_next_package
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.sqlite_migrations import current_schema_version
from aigineering.core.session import runtime_record_worker_references
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.claim_assignments import (
    assignment_context_from_projection,
    effective_assignment,
)
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_assignment import TASK_ASSIGNMENT_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_revocation_effect,
    contract_declaration_effect,
    task_assign_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.runtime_record import create_runtime_record


def _workspace(tmp_path):
    store = SQLiteStore(db_path=str(tmp_path / "assignments.sqlite"))
    signer = Ed25519Signer()
    key = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        ("actor.revoke", "contract.publish", "task.assignment.manage"),
    )
    genesis = create_genesis_manifest("core-assignment-test", (key,), "policy:test")
    initialize_genesis(store, genesis)
    contract = build_control_plane_contract(
        name="assigned task",
        description="assignment writer fixture",
        outputs=("result",),
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="declare-assignment-task",
    )
    assert declared.accepted
    store.register_worker(WorkerRegistration("worker:one", capacity=2))
    store.register_worker(WorkerRegistration("worker:two", capacity=2))
    return store, signer, key, genesis, contract


def test_signed_assignment_is_durable_and_limits_the_claim_writer(tmp_path):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    transition = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        task_assign_effect(contract.id, "worker:one", "", "route this work"),
        idempotency_key="assign-once",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )

    assert transition.accepted
    assignment = next(
        record
        for record in transition.runtime_records
        if record.record_type == "claim.assignment.changed"
    )
    assert assignment.payload["worker_id"] == "worker:one"
    assert store.claim_contract(contract.id, "worker:two") is None
    claim = store.claim_contract(contract.id, "worker:one")
    assert claim is not None

    view = RuntimeProjection(store, store).contract_view(contract)
    assert view.assignment_worker_id == "worker:one"
    assert view.assignment_blocker is None

    reopened = SQLiteStore(db_path=str(tmp_path / "assignments.sqlite"))
    assert reopened.get_runtime_record(assignment.id) == assignment
    reopened.close()


def test_assignment_fact_cannot_enter_through_raw_append(tmp_path):
    store, *_rest = _workspace(tmp_path)
    with pytest.raises(ValueError, match="Candidate ingress commitment"):
        store.append_runtime_record(
            create_runtime_record(
                "claim.assignment.changed",
                {
                    "actor_id": "human:operator",
                    "candidate_id": "candidate:forged",
                    "contract_id": "task:forged",
                    "key_id": "operator-key",
                    "previous_assignment_id": "",
                    "reason": "forged",
                    "worker_id": "worker:one",
                },
            )
        )


def test_default_reducer_does_not_admit_assignment_effect(tmp_path):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    result = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        task_assign_effect(contract.id, "worker:one", "", "unregistered effect"),
        idempotency_key="assignment-without-plugin",
    )
    assert not result.accepted
    assert store.scan_runtime_records(record_type="claim.assignment.changed") == []


def test_outputless_contract_is_not_vacuously_treated_as_complete(tmp_path):
    store, signer, key, genesis, _source = _workspace(tmp_path)
    outputless = build_control_plane_contract(
        name="non-vacuous assignment guard",
        description="an assignment requires a real output obligation",
        outputs=(),
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(outputless),
        idempotency_key="declare-outputless-assignment-target",
    )
    assert declared.accepted
    result = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        task_assign_effect(outputless.id, "worker:one", "", "must have work"),
        idempotency_key="assign-outputless-contract",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )
    assert result.accepted
    assignment = store.scan_runtime_records(record_type="claim.assignment.changed")
    assert len(assignment) == 1
    assert assignment[0][1].payload["contract_id"] == outputless.id


def test_busy_assignee_remains_assigned_but_has_a_capacity_blocker(tmp_path):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    store.register_worker(WorkerRegistration("worker:one", capacity=1, version="2"))
    other = build_control_plane_contract(
        name="worker occupies capacity",
        description="keep the assigned Worker busy",
        outputs=("other-result",),
    )
    assert publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(other),
        idempotency_key="declare-busy-task",
    ).accepted
    assert store.claim_contract(other.id, "worker:one") is not None
    assignment = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        task_assign_effect(contract.id, "worker:one", "", "queued owner"),
        idempotency_key="assign-capacity",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )
    assert assignment.accepted

    view = RuntimeProjection(store, store).contract_view(contract)
    assert view.assignment_worker_id == "worker:one"
    assert view.assignment_blocker == "worker_at_capacity"
    assert "assignment:worker_at_capacity:worker:one" in view.blockers


def test_assignment_reference_helper_reads_only_typed_worker_fields():
    record = create_runtime_record(
        "claim.assignment.changed",
        {
            "actor_id": "human:operator",
            "candidate_id": "candidate:one",
            "contract_id": "task:one",
            "key_id": "operator-key",
            "previous_assignment_id": "",
            "reason": "mentions worker:decoy in prose",
            "worker_id": "worker:selected",
        },
    )
    assert runtime_record_worker_references(record) == frozenset({"worker:selected"})
    assignment = create_runtime_record(
        "claim.assignment.changed",
        {
            "actor_id": "human:operator",
            "candidate_id": "candidate:assignment",
            "contract_id": "task:assignment",
            "key_id": "operator-key",
            "previous_assignment_id": "",
            "reason": "owner selected",
            "worker_id": "worker:causal-owner",
        },
        causal_parents=("receipt:assignment",),
    )
    successor = create_runtime_record(
        "task.handed_off",
        {"contract_id": "task:source", "reason": "mentions worker:decoy"},
        causal_parents=(assignment.id,),
    )
    assert runtime_record_worker_references(
        successor, {assignment.id: assignment}
    ) == frozenset({"worker:causal-owner"})
    decoy_only = create_runtime_record(
        "task.revised",
        {"contract_id": "task:source", "reason": "mentions worker:decoy"},
    )
    assert runtime_record_worker_references(decoy_only) == frozenset()


def test_ordinary_child_declaration_does_not_inherit_assignment(tmp_path):
    store, signer, key, genesis, source = _workspace(tmp_path)
    assigned = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        task_assign_effect(source.id, "worker:one", "", "source owner"),
        idempotency_key="assign-source-for-child-test",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )
    assert assigned.accepted

    child = contract_from_fields(
        name="ordinary child",
        description="declared independently rather than as an assignment successor",
        inputs=(),
        outputs=("child-result",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
        parent_id=source.id,
        worker_capabilities=(),
        worker_pools=(),
        delegation_capabilities=(),
        delegation_pools=(),
        minting_authority=(),
        sensitive_input_policy=None,
        acceptance_policy=None,
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(child),
        idempotency_key="declare-ordinary-child",
    )
    assert declared.accepted
    context = assignment_context_from_projection(load_effect_projection_context(store))
    assert effective_assignment(context, contract_id=source.id) is not None
    assert effective_assignment(context, contract_id=child.id) is None
    assert store.claim_contract(child.id, "worker:two") is not None


@pytest.mark.parametrize("status", ["active", "released", "expired", "submitted"])
def test_any_historical_claim_status_prevents_in_place_assignment(tmp_path, status):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    claim = store.claim_contract(contract.id, "worker:one")
    assert claim is not None
    if status != "active":
        store._conn.execute(
            "UPDATE worker_claims SET status = ? WHERE claim_id = ?",
            (status, claim["claim_id"]),
        )
        store._conn.commit()

    result = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        task_assign_effect(contract.id, "worker:two", "", "must be a successor"),
        idempotency_key=f"assignment-after-{status}-claim",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )
    assert not result.accepted
    assert store.scan_runtime_records(record_type="claim.assignment.changed") == []
    assert contract.id in store.get_claim_history_contract_ids()


def test_schema20_upgrade_refuses_preexisting_assignment_facts(tmp_path):
    database = tmp_path / "schema20.sqlite"
    store = SQLiteStore(db_path=str(database))
    store.close()
    record = create_runtime_record(
        "claim.assignment.changed",
        {
            "actor_id": "human:operator",
            "candidate_id": "candidate:imported",
            "contract_id": "task:imported",
            "key_id": "operator-key",
            "previous_assignment_id": "",
            "reason": "legacy imported fact",
            "worker_id": "worker:one",
        },
    )
    connection = sqlite3.connect(database)
    connection.execute("UPDATE schema_version SET version = 20")
    connection.execute(
        """INSERT INTO runtime_records (
            record_id, record_type, schema_version, payload_json,
            causal_parents, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            record.id,
            record.record_type,
            record.schema_version,
            json.dumps(dict(record.payload), sort_keys=True),
            json.dumps(list(record.causal_parents)),
            record.recorded_at,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="pre-existing task assignment facts"):
        SQLiteStore(db_path=str(database))

    check = sqlite3.connect(database)
    assert current_schema_version(check) == 20
    assert check.execute(
        "SELECT record_id FROM runtime_records WHERE record_id = ?", (record.id,)
    ).fetchone() == (record.id,)
    check.close()


def test_assignment_and_claim_race_is_arbitrated_by_sqlite_writer(
    tmp_path, monkeypatch
):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    other = SQLiteStore(db_path=str(tmp_path / "assignments.sqlite"))
    original = store.commit_ingress_batch
    entered = Event()
    release = Event()

    def gated(*args, **kwargs):
        store.commit_ingress_batch = original
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", gated)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(task_assign_effect(contract.id, "worker:one", "", "race assignment"),),
        signer=signer,
        idempotency_key="assignment-race-candidate",
    )
    outcome = []

    def commit_assignment():
        try:
            outcome.append(
                CandidateCommitter(
                    store, store, additional_effects=TASK_ASSIGNMENT_EFFECTS
                ).commit(candidate, genesis)
            )
        except Exception as exc:  # Captured so worker-thread failures are asserted.
            outcome.append(exc)

    thread = Thread(target=commit_assignment)
    thread.start()
    assert entered.wait(5)
    winning_claim = other.claim_contract(contract.id, "worker:one")
    assert winning_claim is not None
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert not outcome[0].accepted
    rejection = next(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.rejected")
        if record.payload.get("candidate_id") == candidate.id
    )
    assert rejection.payload.get("reason")
    assert store.scan_runtime_records(record_type="claim.assignment.changed") == []
    assert other.get_claim(contract.id) is not None
    other.close()


def test_assignment_first_writer_fences_a_racing_other_worker_claim(
    tmp_path, monkeypatch
):
    tmp_path.mkdir(exist_ok=True)
    store, signer, key, genesis, contract = _workspace(tmp_path)
    other = SQLiteStore(db_path=str(tmp_path / "assignments.sqlite"))
    original = store.commit_ingress_batch
    entered = Event()
    release = Event()
    claim_entered = Event()
    release_claim = Event()

    def gated(*args, **kwargs):
        store.commit_ingress_batch = original
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", gated)
    original_claim = other.claim_contract

    def gated_claim(*args, **kwargs):
        claim_entered.set()
        assert release_claim.wait(5)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(other, "claim_contract", gated_claim)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(task_assign_effect(contract.id, "worker:one", "", "race winner"),),
        signer=signer,
        idempotency_key="assignment-first-race",
    )
    outcome = []
    claim_outcome = []

    def commit_assignment():
        try:
            outcome.append(
                CandidateCommitter(
                    store, store, additional_effects=TASK_ASSIGNMENT_EFFECTS
                ).commit(candidate, genesis)
            )
        except Exception as exc:
            outcome.append(exc)

    def scan_claim():
        try:
            claim_outcome.append(
                claim_next_package(
                    other, worker_id="worker:two", contract_id=contract.id
                )
            )
        except Exception as exc:
            claim_outcome.append(exc)

    thread = Thread(target=commit_assignment)
    claim_thread = Thread(target=scan_claim)
    thread.start()
    assert entered.wait(5)
    claim_thread.start()
    assert claim_entered.wait(5)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(outcome) == 1 and getattr(outcome[0], "accepted", False)
    release_claim.set()
    claim_thread.join(5)
    assert not claim_thread.is_alive()
    assert len(claim_outcome) == 1 and claim_outcome[0] is None
    assert other.get_claim(contract.id) is None
    other.close()


def test_registration_change_between_projection_and_writer_rejects_assignment(
    tmp_path, monkeypatch
):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    other = SQLiteStore(db_path=str(tmp_path / "assignments.sqlite"))
    original = store.commit_ingress_batch
    entered = Event()
    release = Event()

    def gated(*args, **kwargs):
        store.commit_ingress_batch = original
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", gated)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(
            task_assign_effect(contract.id, "worker:one", "", "registration race"),
        ),
        signer=signer,
        idempotency_key="assignment-registration-race",
    )
    outcome = []
    thread = Thread(
        target=lambda: outcome.append(
            CandidateCommitter(
                store, store, additional_effects=TASK_ASSIGNMENT_EFFECTS
            ).commit(candidate, genesis)
        )
    )
    thread.start()
    assert entered.wait(5)
    other.register_worker(
        WorkerRegistration("worker:one", enabled=False, capacity=2, version="2")
    )
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(outcome) == 1 and not outcome[0].accepted
    assert store.scan_runtime_records(record_type="claim.assignment.changed") == []
    assert other.get_worker_registration("worker:one").version == "2"
    other.close()


def test_opposite_reassignments_project_same_head_and_only_one_commits(
    tmp_path, monkeypatch
):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    initial = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        task_assign_effect(contract.id, "worker:one", "", "initial assignment"),
        idempotency_key="reassign-race-initial",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )
    assert initial.accepted
    head = next(
        record
        for record in initial.runtime_records
        if record.record_type == "claim.assignment.changed"
    )
    other = SQLiteStore(db_path=str(tmp_path / "assignments.sqlite"))
    first_original = store.commit_ingress_batch
    second_original = other.commit_ingress_batch
    first_entered, second_entered = Event(), Event()
    first_release, second_release = Event(), Event()

    def first_gate(*args, **kwargs):
        store.commit_ingress_batch = first_original
        first_entered.set()
        assert first_release.wait(5)
        return first_original(*args, **kwargs)

    def second_gate(*args, **kwargs):
        other.commit_ingress_batch = second_original
        second_entered.set()
        assert second_release.wait(5)
        return second_original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", first_gate)
    monkeypatch.setattr(other, "commit_ingress_batch", second_gate)
    candidates = (
        create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=key.actor_id,
            key_id=key.key_id,
            effects=(
                task_assign_effect(contract.id, "worker:two", head.id, "replace owner"),
            ),
            signer=signer,
            idempotency_key="reassign-race-replace",
        ),
        create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=key.actor_id,
            key_id=key.key_id,
            effects=(task_assign_effect(contract.id, "", head.id, "clear owner"),),
            signer=signer,
            idempotency_key="reassign-race-clear",
        ),
    )
    outcomes = [[], []]

    def run(index, selected_store):
        try:
            outcomes[index].append(
                CandidateCommitter(
                    selected_store,
                    selected_store,
                    additional_effects=TASK_ASSIGNMENT_EFFECTS,
                ).commit(candidates[index], genesis)
            )
        except Exception as exc:
            outcomes[index].append(exc)

    first = Thread(target=run, args=(0, store))
    second = Thread(target=run, args=(1, other))
    first.start()
    second.start()
    assert first_entered.wait(5) and second_entered.wait(5)
    first_release.set()
    first.join(5)
    assert not first.is_alive()
    second_release.set()
    second.join(5)
    assert not second.is_alive()
    assert len(outcomes[0]) == len(outcomes[1]) == 1
    assert getattr(outcomes[0][0], "accepted", False)
    assert not getattr(outcomes[1][0], "accepted", True)
    history = tuple(
        record
        for _, record in store.scan_runtime_records(
            record_type="claim.assignment.changed"
        )
    )
    assert len(history) == 2
    assert history[-1].payload["worker_id"] == "worker:two"
    assert not any(
        record.payload.get("candidate_id") == candidates[1].id
        for _, record in other.scan_runtime_records(
            record_type="claim.assignment.changed"
        )
    )
    other.close()


def test_exact_assignment_replay_survives_later_registration_rotation(tmp_path):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(task_assign_effect(contract.id, "worker:one", "", "original owner"),),
        signer=signer,
        idempotency_key="assignment-replay-registration",
    )
    committer = CandidateCommitter(
        store, store, additional_effects=TASK_ASSIGNMENT_EFFECTS
    )
    first = committer.commit(candidate, genesis)
    assert first.accepted
    original = next(
        record
        for record in first.runtime_records
        if record.record_type == "claim.assignment.changed"
    )
    store.register_worker(
        WorkerRegistration("worker:one", enabled=False, capacity=2, version="2")
    )

    replay = committer.commit(candidate, genesis)
    assert replay.accepted
    replayed = next(
        record
        for record in replay.runtime_records
        if record.record_type == "claim.assignment.changed"
    )
    assert replayed.id == original.id
    persisted = store.scan_runtime_records(record_type="claim.assignment.changed")
    assert len(persisted) == 1 and persisted[0][1].id == original.id


def test_operator_key_revocation_between_projection_and_writer_rejects_candidate(
    tmp_path, monkeypatch
):
    store, signer, key, genesis, contract = _workspace(tmp_path)
    other = SQLiteStore(db_path=str(tmp_path / "assignments.sqlite"))
    original = store.commit_ingress_batch
    entered = Event()
    release = Event()

    def gated(*args, **kwargs):
        store.commit_ingress_batch = original
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", gated)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(task_assign_effect(contract.id, "worker:one", "", "key race"),),
        signer=signer,
        idempotency_key="assignment-key-race",
    )
    outcome = []

    def commit_assignment():
        try:
            outcome.append(
                CandidateCommitter(
                    store, store, additional_effects=TASK_ASSIGNMENT_EFFECTS
                ).commit(candidate, genesis)
            )
        except Exception as exc:
            outcome.append(exc)

    thread = Thread(target=commit_assignment)
    thread.start()
    assert entered.wait(5)
    revoked = publish_effect(
        other,
        other,
        genesis,
        key,
        signer,
        actor_revocation_effect(key.actor_id, key.key_id, "operator key rotated"),
        idempotency_key="revoke-assignment-key-during-preflight",
    )
    assert revoked.accepted
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(outcome) == 1 and not getattr(outcome[0], "accepted", True)
    assert store.scan_runtime_records(record_type="claim.assignment.changed") == []
    assert any(
        record.payload.get("claimed_candidate_id") == candidate.id
        for _, record in store.scan_runtime_records(
            record_type="candidate.authentication_rejected"
        )
    )
    other.close()
