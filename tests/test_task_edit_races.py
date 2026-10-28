"""Force edit/claim/control races between independent SQLite connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
from aigineering.plugins.handoff import HANDOFF_EFFECTS, task_edit_snapshot
from aigineering.plugins.task_assignment import TASK_ASSIGNMENT_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    task_assign_effect,
    task_block_effect,
    task_handoff_effect,
    task_unblock_effect,
    worker_registration_effect,
)
from aigineering.runtime import claim_next_package


def _workspace(tmp_path):
    path = tmp_path / "edit-races.sqlite"
    store, other = SQLiteStore(path), SQLiteStore(path)
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:edit-race",
        "edit-race-key",
        signer.kind,
        signer.signer_id,
        (
            "contract.publish",
            "contract.cancel",
            "task.handoff",
            "task.edit",
            "task.block",
            "task.unblock",
            "task.assignment.manage",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest("edit-races", (actor,), "policy:edit-races")
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="Before", description="Instructions", outputs=("result",), budget=5
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-edit-race-source",
    )
    assert declared.accepted
    declaration = next(
        record
        for record in declared.runtime_records
        if record.record_type == "contract.declared"
    )
    return store, other, signer, actor, genesis, source, declaration


def _edit(store, signer, actor, genesis, source, declaration, key, title="After"):
    assignment, holds = task_edit_snapshot(
        source, load_effect_projection_context(store)
    )
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        signer=signer,
        idempotency_key=key,
        effects=(
            task_handoff_effect(
                source.id,
                "Correct instructions",
                edit={
                    "title": title,
                    "source_assignment_id": assignment,
                    "source_hold_ids": holds,
                },
            ),
        ),
        causal_parents=(
            source.id,
            declaration.id,
            *((assignment,) if assignment else ()),
            *holds,
        ),
    )


def _commit(store, genesis, candidate):
    return CandidateCommitter(store, store, additional_effects=HANDOFF_EFFECTS).commit(
        candidate, genesis
    )


def _pause_edit_writer(store, monkeypatch):
    reached, release = Event(), Event()
    original = store.commit_ingress_batch

    def paused(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and any(
            record.record_type == "task.handed_off"
            for record in kwargs.get("runtime_records", ())
        ):
            reached.set()
            assert release.wait(10), "edit writer barrier timed out"
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", paused)
    return reached, release


@pytest.mark.parametrize("change", ("place", "release", "assignment", "edit"))
def test_source_change_after_edit_projection_rejects_atomically(
    tmp_path, monkeypatch, change
):
    store, other, signer, actor, genesis, source, declaration = _workspace(tmp_path)
    try:
        hold_id = ""
        assignment_id = ""
        if change == "assignment":
            registration = publish_effect(
                other,
                other,
                genesis,
                actor,
                signer,
                worker_registration_effect(
                    WorkerRegistration(
                        actor.actor_id, actor_id=actor.actor_id, key_id=actor.key_id
                    )
                ),
                idempotency_key="register-edit-race-assignee",
            )
            assert registration.accepted
            assigned = publish_effect(
                other,
                other,
                genesis,
                actor,
                signer,
                task_assign_effect(source.id, actor.actor_id, "", "Initial assignee"),
                idempotency_key="initial-edit-race-assignment",
                additional_effects=TASK_ASSIGNMENT_EFFECTS,
            )
            assert assigned.accepted
            assignment_id = next(
                record.id
                for record in assigned.runtime_records
                if record.record_type == "claim.assignment.changed"
            )
        if change == "release":
            placed = publish_effect(
                other,
                other,
                genesis,
                actor,
                signer,
                task_block_effect(source.id, "Before snapshot"),
                idempotency_key="initial-edit-race-hold",
                additional_effects=COLLABORATION_EFFECTS,
            )
            assert placed.accepted
            hold_id = next(
                record.id
                for record in placed.runtime_records
                if record.record_type == "claim.hold.placed"
            )
        candidate = _edit(
            store, signer, actor, genesis, source, declaration, "stale-edit"
        )
        reached, release = _pause_edit_writer(store, monkeypatch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_commit, store, genesis, candidate)
            try:
                assert reached.wait(10), "edit did not reach writer boundary"
                if change == "edit":
                    winner = _commit(
                        other,
                        genesis,
                        _edit(
                            other,
                            signer,
                            actor,
                            genesis,
                            source,
                            declaration,
                            "winning-edit",
                            "Winning instructions",
                        ),
                    )
                else:
                    effect, registry = {
                        "place": (
                            task_block_effect(source.id, "After snapshot"),
                            COLLABORATION_EFFECTS,
                        ),
                        "release": (
                            task_unblock_effect(source.id, hold_id, "Cleanup"),
                            COLLABORATION_EFFECTS,
                        ),
                        "assignment": (
                            task_assign_effect(
                                source.id, "", assignment_id, "Clear after snapshot"
                            ),
                            TASK_ASSIGNMENT_EFFECTS,
                        ),
                    }[change]
                    winner = publish_effect(
                        other,
                        other,
                        genesis,
                        actor,
                        signer,
                        effect,
                        idempotency_key=f"control-after-edit-snapshot:{change}",
                        additional_effects=registry,
                    )
                assert winner.accepted, winner
            finally:
                release.set()
            outcome = future.result(timeout=10)
        assert not outcome.accepted
        records = tuple(record for _, record in store.scan_runtime_records())
        rejected = next(
            record
            for record in records
            if record.record_type == "candidate.rejected"
            and record.payload.get("candidate_id") == candidate.id
        )
        expected_reason = {
            "place": "source hold set changed",
            "release": "source hold set changed",
            "assignment": "source assignment head changed",
            "edit": "already terminal",
        }[change]
        assert expected_reason in rejected.payload["reason"]
        assert not any(
            record.payload.get("candidate_id") == candidate.id
            and record.record_type
            in {"task.handed_off", "lifecycle.terminal", "claim.hold.placed"}
            for record in records
        )
        assert len(store.get_all_contracts()) == (2 if change == "edit" else 1)
    finally:
        other.close()
        store.close()


def test_claim_winning_after_edit_projection_is_cancelled_atomically(
    tmp_path, monkeypatch
):
    store, other, signer, actor, genesis, source, declaration = _workspace(tmp_path)
    try:
        candidate = _edit(
            store, signer, actor, genesis, source, declaration, "claim-first-edit"
        )
        reached, release = _pause_edit_writer(store, monkeypatch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_commit, store, genesis, candidate)
            try:
                assert reached.wait(10)
                claim = other.claim_contract(source.id, "worker:before-edit")
                assert claim is not None
            finally:
                release.set()
            outcome = future.result(timeout=10)
        assert outcome.accepted
        assert other.get_claim(source.id)["status"] != "active"
        assert other.claim_contract(source.id, "worker:after-edit") is None
        terminals = store.scan_runtime_records(record_type="lifecycle.terminal")
        assert any(
            record.payload.get("contract_id") == source.id for _, record in terminals
        )
    finally:
        other.close()
        store.close()


def test_edit_winning_after_claim_preflight_fences_the_claim_writer(
    tmp_path, monkeypatch
):
    store, other, signer, actor, genesis, source, declaration = _workspace(tmp_path)
    reached, release = Event(), Event()
    original = other.claim_contract

    def paused_claim(*args, **kwargs):
        reached.set()
        assert release.wait(10), "claim writer barrier timed out"
        return original(*args, **kwargs)

    monkeypatch.setattr(other, "claim_contract", paused_claim)
    try:
        candidate = _edit(
            store, signer, actor, genesis, source, declaration, "edit-first"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                claim_next_package,
                other,
                worker_id="worker:stale-preflight",
                contract_id=source.id,
            )
            try:
                assert reached.wait(10), "claim did not reach writer entry"
                assert _commit(store, genesis, candidate).accepted
            finally:
                release.set()
            assert future.result(timeout=10) is None
        assert other.get_claim(source.id) is None
    finally:
        other.close()
        store.close()
