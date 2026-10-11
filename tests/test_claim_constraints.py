from __future__ import annotations

import sqlite3

import pytest
from conftest import candidate_runtime

from aigineering.core.commitment import CandidateCommitRejected, CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.claim_constraints import (
    ClaimConstraintConflict,
    claim_constraint_blockers,
    claim_constraint_history,
    external_prerequisite_blockers,
    open_claim_constraints,
    validate_claim_constraint_transition,
)
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.candidate import create_candidate_proposal
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    task_dependency_link_effect,
)
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_to_dict
from aigineering.task_views import project_task_status
from aigineering.task_view_query import load_task_view_query_context


def _placement(target: str, prerequisite: str, candidate: str = "candidate-1"):
    receipt_id = f"receipt:{candidate}"
    return create_runtime_record(
        "claim.constraint.placed",
        {
            "actor_id": "operator",
            "key_id": "operator-key",
            "candidate_id": candidate,
            "contract_id": target,
            "prerequisite_contract_id": prerequisite,
            "predicate": "prerequisite_terminal",
            "required_terminal": "complete",
            "reason": "Run after prerequisite",
        },
        causal_parents=(receipt_id,),
    )


def _commit_transition(store, transition, *, candidate_id: str):
    actor_id = str(transition.payload["actor_id"])
    key_id = str(transition.payload["key_id"])
    receipt = create_runtime_record(
        "candidate.received",
        {"candidate_id": candidate_id, "actor_id": actor_id, "key_id": key_id},
    )
    transition = create_runtime_record(
        transition.record_type,
        {**dict(transition.payload), "candidate_id": candidate_id},
        causal_parents=(
            (receipt.id, str(transition.payload["constraint_id"]))
            if transition.record_type == "claim.constraint.released"
            else (receipt.id,)
        ),
    )
    head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": candidate_id, "committed_record_ids": [transition.id]},
        causal_parents=(receipt.id, transition.id),
    )
    store.commit_ingress_batch(
        [],
        [],
        runtime_records=(receipt, transition, head),
        candidate_id=candidate_id,
        candidate_actor_id=actor_id,
        candidate_key_id=key_id,
    )
    return transition


def _release(placement, candidate_id: str = "candidate-2"):
    return create_runtime_record(
        "claim.constraint.released",
        {
            **dict(placement.payload),
            "candidate_id": candidate_id,
            "reason": "Dependency was replaced",
            "constraint_id": placement.id,
        },
        causal_parents=(f"receipt:{candidate_id}", placement.id),
    )


def test_typed_complete_is_the_only_satisfied_prerequisite_state():
    edge = _placement("target", "pre")
    assert claim_constraint_blockers((edge,), contract_id="target")[0] == {
        "contract_id": "target",
        "constraint_id": edge.id,
        "prerequisite_contract_id": "pre",
        "predicate": "prerequisite_terminal",
        "required_terminal": "complete",
        "observed_terminal": "missing",
    }
    complete = create_terminal_record("pre", "complete")
    assert claim_constraint_blockers((edge, complete), contract_id="target") == ()
    for state in ("failed", "cancelled", "unreachable"):
        terminal = create_terminal_record("pre", state)
        assert (
            claim_constraint_blockers((edge, terminal), contract_id="target")[0][
                "observed_terminal"
            ]
            == state
        )


def test_blocker_checks_ignore_unrelated_malformed_terminal_facts():
    unrelated = create_runtime_record(
        "lifecycle.terminal", {"contract_id": "unrelated", "terminal": []}
    )
    assert claim_constraint_blockers((unrelated,), contract_id="target") == ()

    edge = _placement("target", "pre")
    assert (
        claim_constraint_blockers((edge, unrelated), contract_id="target")[0][
            "observed_terminal"
        ]
        == "missing"
    )

    relevant = create_runtime_record(
        "lifecycle.terminal", {"contract_id": "pre", "terminal": []}
    )
    with pytest.raises(ClaimConstraintConflict, match="typed prerequisite terminal"):
        claim_constraint_blockers((edge, relevant), contract_id="target")
    with pytest.raises(ClaimConstraintConflict, match="typed prerequisite terminal"):
        claim_constraint_history((edge, relevant), contract_id="target")

    assert (
        external_prerequisite_blockers(
            (unrelated,), cancelled_contract_ids=frozenset({"target"})
        )
        == ()
    )


def test_projection_exposes_edge_as_blocker_and_rebuildable_hash_input():
    store = SQLiteStore(":memory:")
    target = Contract(id="target", outputs=("result",), budget=2)
    prerequisite = Contract(id="pre", outputs=("source",), budget=2)
    store.add_contract(target)
    store.add_contract(prerequisite)
    edge = _placement(target.id, prerequisite.id)
    _commit_transition(store, edge, candidate_id="candidate-1")
    edge = store.scan_runtime_records(record_type="claim.constraint.placed")[0][1]
    view = RuntimeProjection(store, store).contract_view(target)
    assert view.enabled is False
    assert view.blockers == (f"prerequisite:{edge.id}:pre:missing",)
    legacy_status = project_task_status(target, store)
    query_context = load_task_view_query_context(store)
    assert (
        project_task_status(
            target,
            store,
            projection_context=query_context.effect_projection,
            query_context=query_context,
        )
        == legacy_status
    )
    before = view.projection_hash
    store.append_runtime_record(create_terminal_record(prerequisite.id, "complete"))
    ready = RuntimeProjection(store, store).contract_view(target)
    assert ready.enabled is True
    assert ready.projection_hash != before
    legacy_ready = project_task_status(target, store)
    ready_context = load_task_view_query_context(store)
    assert (
        project_task_status(
            target,
            store,
            projection_context=ready_context.effect_projection,
            query_context=ready_context,
        )
        == legacy_ready
    )
    records = tuple(record for _, record in store.scan_runtime_records())
    assert (
        claim_constraint_history(records, contract_id=target.id)[0]["observed_terminal"]
        == "complete"
    )
    store.close()


def test_unconstrained_projection_hash_ignores_unrelated_terminal_facts():
    store = SQLiteStore(":memory:")
    target = Contract(id="target", outputs=("result",), budget=2)
    store.add_contract(target)
    store.add_contract(Contract(id="unrelated", outputs=("other",), budget=1))
    before = RuntimeProjection(store, store).contract_view(target).projection_hash
    store.append_runtime_record(create_terminal_record("unrelated", "complete"))
    after = RuntimeProjection(store, store).contract_view(target).projection_hash
    assert after == before
    store.close()


def test_as_of_revision_does_not_observe_a_future_prerequisite_terminal():
    store = SQLiteStore(":memory:")
    target = Contract(id="target", outputs=("result",), budget=2)
    prerequisite = Contract(id="pre", outputs=("source",), budget=2)
    store.add_contract(target)
    store.add_contract(prerequisite)
    store.append_runtime_record(
        create_runtime_record(
            "contract.declared", {"contract": contract_to_dict(target)}
        )
    )
    store.append_runtime_record(
        create_runtime_record(
            "contract.declared", {"contract": contract_to_dict(prerequisite)}
        )
    )
    edge = _placement(target.id, prerequisite.id)
    _commit_transition(store, edge, candidate_id="candidate-1")
    edge_revision = store.get_runtime_revision()
    store.append_runtime_record(create_terminal_record(prerequisite.id, "complete"))

    historical = RuntimeProjection(
        store, store, as_of_revision=edge_revision
    ).contract_view(target)
    current = RuntimeProjection(store, store).contract_view(target)
    assert any(item.endswith(":pre:missing") for item in historical.blockers)
    assert not any(item.startswith("prerequisite:") for item in current.blockers)
    store.close()


def test_exact_unlink_releases_one_open_edge_without_rewriting_history():
    store = SQLiteStore(":memory:")
    target = Contract(id="target", outputs=("result",), budget=2)
    prerequisite = Contract(id="pre", budget=2)
    store.add_contract(target)
    store.add_contract(prerequisite)
    placement = _placement(target.id, prerequisite.id)
    _commit_transition(store, placement, candidate_id="candidate-1")
    placement = store.scan_runtime_records(record_type="claim.constraint.placed")[0][1]
    release = _release(placement)
    _commit_transition(store, release, candidate_id="candidate-2")
    release = store.scan_runtime_records(record_type="claim.constraint.released")[0][1]
    all_records = tuple(record for _, record in store.scan_runtime_records())
    assert open_claim_constraints(all_records) == ()
    assert store.scan_runtime_records(record_type="claim.constraint.placed")
    assert store.scan_runtime_records(record_type="claim.constraint.released")
    history = claim_constraint_history(all_records, contract_id=target.id)
    assert len(history) == 1
    assert history[0]["constraint_id"] == placement.id
    assert history[0]["released_record_id"] == release.id
    assert history[0]["release_reason"] == "Dependency was replaced"
    assert history[0]["active"] is False
    assert store.claim_contract(target.id, "worker") is not None
    store.close()


def test_sqlite_claim_gate_blocks_until_typed_prerequisite_completion():
    store = SQLiteStore(":memory:")
    target = Contract(id="target", outputs=("result",), budget=2)
    prerequisite = Contract(id="pre", outputs=("source",), budget=2)
    store.add_contract(target)
    store.add_contract(prerequisite)
    placement = _placement(target.id, prerequisite.id)
    _commit_transition(store, placement, candidate_id="candidate-1")
    assert store.claim_contract(target.id, "worker") is None
    store.append_runtime_record(create_terminal_record(prerequisite.id, "complete"))
    assert store.claim_contract(target.id, "worker") is not None
    store.close()


def test_constraint_cycles_are_rejected_in_the_pure_transition_owner():
    first = _placement("a", "b", "candidate-a")
    second = _placement("b", "a", "candidate-b")
    with pytest.raises(ClaimConstraintConflict, match="cycle"):
        validate_claim_constraint_transition(
            (first,),
            second,
            known_contract_ids=frozenset({"a", "b"}),
            claimed_contract_ids=frozenset(),
            terminal_contract_ids=frozenset(),
        )


def test_writer_rejects_claimed_or_output_satisfied_targets():
    prerequisite = "pre"
    transition = _placement("target", prerequisite)
    common = {
        "known_contract_ids": frozenset({"target", prerequisite}),
        "terminal_contract_ids": frozenset(),
    }
    with pytest.raises(ClaimConstraintConflict, match="claim history"):
        validate_claim_constraint_transition(
            (), transition, claimed_contract_ids=frozenset({"target"}), **common
        )
    with pytest.raises(ClaimConstraintConflict, match="outputs are already satisfied"):
        validate_claim_constraint_transition(
            (),
            transition,
            claimed_contract_ids=frozenset(),
            output_satisfied_contract_ids=frozenset({"target"}),
            **common,
        )


def test_external_successor_guard_is_directional_and_repair_aware():
    crossing = _placement(
        "cancelled-target",
        "live-prerequisite",
    )
    crossing = create_runtime_record(
        crossing.record_type,
        {
            **dict(crossing.payload),
            "reason": "reason mentions task:unrelated-fake-id",
        },
        causal_parents=crossing.causal_parents,
    )
    internal = _placement("cancelled-target", "cancelled-peer", "candidate-2")
    records = (crossing, internal)
    blockers = external_prerequisite_blockers(
        records,
        cancelled_contract_ids=frozenset({"cancelled-target", "cancelled-peer"}),
    )
    assert len(blockers) == 1
    assert blockers[0]["constraint_id"] == crossing.id
    from aigineering.core.session import runtime_record_contract_references

    assert runtime_record_contract_references(crossing) == frozenset(
        {"cancelled-target", "live-prerequisite"}
    )
    assert (
        external_prerequisite_blockers(
            records,
            cancelled_contract_ids=frozenset({"cancelled-target", "cancelled-peer"}),
            repair_complete_contract_ids=frozenset({"live-prerequisite"}),
        )
        == ()
    )
    assert (
        external_prerequisite_blockers(
            records,
            cancelled_contract_ids=frozenset({"cancelled-target", "cancelled-peer"}),
            repair_only_target_ids=frozenset({"cancelled-target"}),
        )
        == ()
    )


def test_raw_append_cannot_publish_reserved_constraint_facts():
    record = _placement("target", "pre")
    for store in (MemoryStore(), SQLiteStore(":memory:")):
        with pytest.raises(ValueError, match="Candidate ingress"):
            store.append_runtime_record(record)
        if isinstance(store, SQLiteStore):
            store.close()


def test_writer_constraint_conflict_becomes_a_durable_candidate_rejection(
    tmp_path,
):
    store = SQLiteStore(str(tmp_path / "durable-rejection.db"))
    runtime = candidate_runtime(store)
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(
            contract_declaration_effect(
                build_control_plane_contract(
                    name="writer rejection", description="test"
                )
            ),
        ),
        signer=runtime.signer,
        idempotency_key="constraint-writer-conflict",
    )
    original_commit = store.commit_ingress_batch
    conflict_once = True

    def reject_first_commit(*args, **kwargs):
        nonlocal conflict_once
        if conflict_once:
            conflict_once = False
            raise ClaimConstraintConflict("target has claim history")
        return original_commit(*args, **kwargs)

    store.commit_ingress_batch = reject_first_commit
    with pytest.raises(CandidateCommitRejected, match="target has claim history"):
        CandidateCommitter(store, store).commit(candidate, runtime.genesis)
    assert store.scan_runtime_records(record_type="candidate.rejected")
    assert not store.scan_runtime_records(record_type="contract.declared")
    store.close()


def test_second_connection_claim_wins_between_projection_and_writer(tmp_path):
    from aigineering.plugins.task_dependencies import TASK_DEPENDENCY_EFFECTS

    database = str(tmp_path / "constraint-claim-race.db")
    store = SQLiteStore(database)
    claim_store = SQLiteStore(database)
    signer = Ed25519Signer()
    actor_key = ActorKey(
        "operator:dependencies",
        "dependency-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "task.dependency.manage"),
    )
    genesis = create_genesis_manifest(
        "claim-constraint-race", (actor_key,), "policy:test"
    )
    initialize_genesis(store, genesis)
    target = Contract(id="target-race", outputs=("result",), budget=2)
    prerequisite = Contract(id="pre-race", outputs=("input",), budget=2)
    store.add_contract(target)
    store.add_contract(prerequisite)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        effects=(task_dependency_link_effect(target.id, prerequisite.id, "wait"),),
        signer=signer,
        idempotency_key="link-after-claim-window",
    )
    original_commit = store.commit_ingress_batch
    injected = False

    def claim_between_projection_and_writer(*args, **kwargs):
        nonlocal injected
        if not injected and any(
            record.record_type == "claim.constraint.placed"
            for record in kwargs.get("runtime_records", ())
        ):
            injected = True
            assert claim_store.claim_contract(target.id, "worker:race") is not None
        return original_commit(*args, **kwargs)

    store.commit_ingress_batch = claim_between_projection_and_writer
    committer = CandidateCommitter(
        store, store, additional_effects=TASK_DEPENDENCY_EFFECTS
    )
    decision = committer.commit(candidate, genesis)
    assert injected
    assert not decision.accepted
    assert claim_store.get_claim(target.id)["status"] == "active"
    assert not store.scan_runtime_records(record_type="claim.constraint.placed")
    rejected = store.scan_runtime_records(record_type="candidate.rejected")
    assert len(rejected) == 1
    assert "claim history" in rejected[0][1].payload["reason"]
    claim_store.close()
    store.close()


def test_schema19_upgrades_to21_but_old_reader_facts_block_upgrade(tmp_path):
    db = tmp_path / "schema19.db"
    store = SQLiteStore(str(db))
    store._conn.execute("UPDATE schema_version SET version = 19")
    store.close()
    reopened = SQLiteStore(str(db))
    assert reopened.schema_version == 21
    reopened.close()

    unsupported = tmp_path / "constraint-import.db"
    store = SQLiteStore(str(unsupported))
    record = _placement("target", "pre")
    with store._conn:
        store._insert_runtime_record(record, allow_claim_constraint_fact=True)
        store._conn.execute("UPDATE schema_version SET version = 19")
    store.close()
    with pytest.raises(ValueError, match="pre-existing claim constraint facts"):
        SQLiteStore(str(unsupported))
    # The failed migration rolls back its version marker.
    connection = sqlite3.connect(unsupported)
    try:
        assert (
            connection.execute("SELECT version FROM schema_version").fetchone()[0] == 19
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM runtime_records WHERE record_type = ?",
                ("claim.constraint.placed",),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
