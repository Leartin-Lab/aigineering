"""Two-connection writer-boundary tests for task revision arbitration."""

from __future__ import annotations

from threading import Event, Thread

from aigineering.core.commitment import CandidateCommitter
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.task_revision import TASK_REVISION_EFFECTS
from aigineering.plugins.task_review import TASK_REVIEW_EFFECTS
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    actor_revocation_effect,
    contract_cancellation_effect,
    task_revision_effect,
)
from aigineering.protocol.candidate import ActorKey

from test_task_revision import _candidate, _commit, _env


_REVISION_EFFECTS = {**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS}


def _pause_at_writer_gate(store):
    reached = Event()
    release = Event()
    original = store.commit_ingress_batch

    def pause_before_write(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and not reached.is_set():
            reached.set()
            if not release.wait(10):
                raise TimeoutError("revision contention release timed out")
        return original(*args, **kwargs)

    store.commit_ingress_batch = pause_before_write
    return reached, release


def _start_revision(env, candidate):
    result = []
    errors = []

    def run():
        try:
            result.append(
                CandidateCommitter(
                    env["store"], env["store"], additional_effects=_REVISION_EFFECTS
                ).commit(candidate, env["genesis"])
            )
        except Exception as exc:  # surfaced in the parent test thread
            errors.append(exc)

    thread = Thread(target=run)
    thread.start()
    return thread, result, errors


def _assert_rejected_candidate(store, candidate_id):
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate_id
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.rejected"
        )
    )


def test_source_cancellation_committed_at_writer_gate_rejects_revision(tmp_path):
    env = _env(tmp_path)
    store = env["store"]
    competing = SQLiteStore(store._db_path)
    revision_candidate = _candidate(
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Revise after source cancellation"),
        "revision-loses-to-source-cancel",
    )
    initial_grants = {
        record.id
        for _revision, record in store.scan_runtime_records(
            record_type="allowance.granted"
        )
    }
    reached, release = _pause_at_writer_gate(store)
    thread, decisions, errors = _start_revision(env, revision_candidate)

    assert reached.wait(10), "revision never reached commit-time snapshot gate"
    try:
        cancelled = _commit(
            competing,
            env["genesis"],
            env["signer"],
            env["operator"],
            contract_cancellation_effect(env["source"].id, "Independent cancel won"),
            "source-cancel-wins-before-revision",
        )
        assert cancelled.accepted, cancelled
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert not errors, errors
    assert len(decisions) == 1 and not decisions[0].accepted
    _assert_rejected_candidate(store, revision_candidate.id)
    assert not store.scan_runtime_records(record_type="task.revised")
    assert not any(
        contract.name and ".revision." in contract.name
        for contract in store.get_all_contracts()
    )
    source = store.get_contract(env["source"].id)
    assert source is not None
    assert RuntimeProjection(store, store).contract_view(source).terminal == "cancelled"
    assert {
        record.id
        for _revision, record in store.scan_runtime_records(
            record_type="allowance.granted"
        )
    } == initial_grants
    competing.close()
    store.close()


def test_one_of_two_revision_candidates_wins_and_only_one_child_reserves_allowance(
    tmp_path,
):
    env = _env(tmp_path)
    store = env["store"]
    competing = SQLiteStore(store._db_path)
    stale_candidate = _candidate(
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Stale competing revision", budget=1),
        "losing-revision-candidate",
    )
    winning_candidate = _candidate(
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Winning bounded revision", budget=2),
        "winning-revision-candidate",
    )
    reached, release = _pause_at_writer_gate(store)
    thread, decisions, errors = _start_revision(env, stale_candidate)

    assert reached.wait(10), "revision never reached commit-time snapshot gate"
    try:
        winner = CandidateCommitter(
            competing, competing, additional_effects=_REVISION_EFFECTS
        ).commit(winning_candidate, env["genesis"])
        assert winner.accepted, winner
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert not errors, errors
    assert len(decisions) == 1 and not decisions[0].accepted
    _assert_rejected_candidate(store, stale_candidate.id)

    links = store.scan_runtime_records(record_type="task.revised")
    assert len(links) == 1
    link = links[0][1]
    assert link.payload["candidate_id"] == winning_candidate.id
    assert link.payload["reason"] == "Winning bounded revision"
    assert link.payload["budget"] == 2
    successor = store.get_contract(link.payload["successor_id"])
    assert successor is not None and successor.parent_id == env["source"].id
    child_ids = {
        contract.id
        for contract in store.get_all_contracts()
        if contract.parent_id == env["source"].id
        and contract.name
        and ".revision." in contract.name
    }
    assert child_ids == {successor.id}
    grants = [
        record
        for _revision, record in store.scan_runtime_records(
            record_type="allowance.granted"
        )
        if record.payload.get("contract_id") == successor.id
    ]
    assert len(grants) == 1
    assert grants[0].payload["amount"] == 2
    competing.close()
    store.close()


def test_revoked_revision_actor_is_rejected_without_receipt_or_child(tmp_path):
    env = _env(tmp_path)
    store = env["store"]
    authority = env["genesis"].root_keys[0]
    revoker = ActorKey(
        "test:revision-revoker",
        "revision-revoker-key",
        env["signer"].kind,
        env["signer"].signer_id,
        ("actor.revoke",),
    )
    authorized = _commit(
        store,
        env["genesis"],
        env["signer"],
        authority,
        actor_authorization_effect(revoker),
        "authorize-revision-revoker",
    )
    assert authorized.accepted, authorized
    revoked = _commit(
        store,
        env["genesis"],
        env["signer"],
        revoker,
        actor_revocation_effect(
            env["operator"].actor_id,
            env["operator"].key_id,
            "Operator revision authority withdrawn",
        ),
        "revoke-revision-operator",
    )
    assert revoked.accepted, revoked

    candidate = _candidate(
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Try with revoked key"),
        "revision-after-revocation",
    )
    result = CandidateCommitter(
        store, store, additional_effects=_REVISION_EFFECTS
    ).commit(candidate, env["genesis"])
    assert not result.accepted
    assert any(
        record.record_type == "candidate.authentication_rejected"
        and record.payload.get("claimed_candidate_id") == candidate.id
        for record in result.runtime_records
    )
    assert not any(
        record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.received"
        )
    )
    assert not store.scan_runtime_records(record_type="task.revised")
    assert not any(
        contract.name and ".revision." in contract.name
        for contract in store.get_all_contracts()
    )
    store.close()
