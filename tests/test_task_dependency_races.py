"""Two-connection writer arbitration for prerequisite constraints."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from aigineering.core.commitment import CandidateCommitRejected, CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.task_dependencies import TASK_DEPENDENCY_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import task_dependency_link_effect
from aigineering.protocol.types import Contract


def _workspace(path):
    first = SQLiteStore(str(path))
    second = SQLiteStore(str(path))
    signer = Ed25519Signer()
    actor_key = ActorKey(
        "operator:dependency-race",
        "dependency-race-key",
        signer.kind,
        signer.signer_id,
        ("task.dependency.manage",),
    )
    from aigineering.protocol.candidate import create_genesis_manifest

    genesis = create_genesis_manifest(
        "dependency-writer-race", (actor_key,), "policy:writer-race"
    )
    initialize_genesis(first, genesis)
    left = Contract(
        id="task:race:left", name="left", outputs=("left-result",), budget=2
    )
    right = Contract(
        id="task:race:right", name="right", outputs=("right-result",), budget=2
    )
    first.add_contract(left)
    first.add_contract(right)
    return first, second, signer, actor_key, genesis, left, right


def _candidate(genesis, signer, actor_key, target, prerequisite, key):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        effects=(
            task_dependency_link_effect(target, prerequisite, "wait for exact task"),
        ),
        signer=signer,
        idempotency_key=key,
    )


def _commit(store, genesis, candidate):
    try:
        return CandidateCommitter(
            store, store, additional_effects=TASK_DEPENDENCY_EFFECTS
        ).commit(candidate, genesis)
    except CandidateCommitRejected:
        return None


def test_opposite_links_race_commits_one_edge_and_durably_rejects_cycle(tmp_path):
    first, second, signer, actor_key, genesis, left, right = _workspace(
        tmp_path / "opposite-dependencies.db"
    )
    candidates = (
        _candidate(genesis, signer, actor_key, left.id, right.id, "left-waits-right"),
        _candidate(genesis, signer, actor_key, right.id, left.id, "right-waits-left"),
    )
    barrier = Barrier(2)
    stores = (first, second)
    original_commits = tuple(store.commit_ingress_batch for store in stores)

    def gated(original):
        def commit(*args, **kwargs):
            if any(
                record.record_type == "claim.constraint.placed"
                for record in kwargs.get("runtime_records", ())
            ):
                barrier.wait(timeout=10)
            return original(*args, **kwargs)

        return commit

    for store, original in zip(stores, original_commits, strict=True):
        store.commit_ingress_batch = gated(original)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: _commit(pair[0], genesis, pair[1]),
                zip(stores, candidates, strict=True),
            )
        )
    assert sum(bool(item and item.accepted) for item in outcomes) == 1
    records = tuple(record for _, record in first.scan_runtime_records())
    placements = [
        record for record in records if record.record_type == "claim.constraint.placed"
    ]
    assert len(placements) == 1
    committed_ids = {
        record.payload["candidate_id"]
        for record in records
        if record.record_type == "candidate.committed"
    }
    rejected_ids = {
        record.payload["candidate_id"]
        for record in records
        if record.record_type == "candidate.rejected"
    }
    winner = next(
        candidate for candidate in candidates if candidate.id in committed_ids
    )
    loser = next(candidate for candidate in candidates if candidate.id in rejected_ids)
    assert winner.id != loser.id
    rejection = next(
        record
        for record in records
        if record.record_type == "candidate.rejected"
        and record.payload["candidate_id"] == loser.id
    )
    assert "cycle" in rejection.payload["reason"].lower()
    assert not any(record.payload["candidate_id"] == loser.id for record in placements)
    first.close()
    second.close()


def test_claim_that_wins_between_link_projection_and_writer_is_durable(tmp_path):
    first, second, signer, actor_key, genesis, target, prerequisite = _workspace(
        tmp_path / "claim-first-dependency.db"
    )
    candidate = _candidate(
        genesis, signer, actor_key, target.id, prerequisite.id, "link-after-claim"
    )
    projected = Event()
    continue_write = Event()
    original = first.commit_ingress_batch

    def pause_link(*args, **kwargs):
        if any(
            record.record_type == "claim.constraint.placed"
            for record in kwargs.get("runtime_records", ())
        ):
            projected.set()
            assert continue_write.wait(timeout=10)
        return original(*args, **kwargs)

    first.commit_ingress_batch = pause_link
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_commit, first, genesis, candidate)
        assert projected.wait(timeout=10)
        claim = second.claim_contract(target.id, "worker:claim-first")
        assert claim is not None
        continue_write.set()
        outcome = future.result(timeout=10)
    assert outcome is None or not outcome.accepted
    assert second.get_claim(target.id)["status"] == "active"
    assert not first.scan_runtime_records(record_type="claim.constraint.placed")
    assert any(
        record.payload["candidate_id"] == candidate.id
        for _, record in first.scan_runtime_records(record_type="candidate.rejected")
    )
    first.close()
    second.close()


def test_link_that_wins_before_claim_blocks_the_second_connection(tmp_path):
    first, second, signer, actor_key, genesis, target, prerequisite = _workspace(
        tmp_path / "link-first-dependency.db"
    )
    candidate = _candidate(
        genesis, signer, actor_key, target.id, prerequisite.id, "link-before-claim"
    )
    outcome = _commit(first, genesis, candidate)
    assert outcome is not None and outcome.accepted
    assert second.claim_contract(target.id, "worker:link-after") is None
    assert len(first.scan_runtime_records(record_type="claim.constraint.placed")) == 1
    first.close()
    second.close()


def test_dependency_effect_requires_capability_and_rejects_mixed_candidate(tmp_path):
    first, second, signer, actor_key, genesis, left, right = _workspace(
        tmp_path / "dependency-auth-boundary.db"
    )
    unauthorized_signer = Ed25519Signer()
    unauthorized_key = ActorKey(
        "operator:without-dependency-capability",
        "no-dependency-key",
        unauthorized_signer.kind,
        unauthorized_signer.signer_id,
        ("contract.publish",),
    )
    unauthorized_genesis = create_genesis_manifest(
        "dependency-unauthorized", (unauthorized_key,), "policy:unauthorized"
    )
    unauthorized_store = SQLiteStore(str(tmp_path / "dependency-unauthorized.db"))
    initialize_genesis(unauthorized_store, unauthorized_genesis)
    unauthorized_store.add_contract(left)
    unauthorized_store.add_contract(right)
    effect = task_dependency_link_effect(left.id, right.id, "must be authorized")
    unauthorized_candidate = create_candidate_proposal(
        domain_id=unauthorized_genesis.id,
        actor_id=unauthorized_key.actor_id,
        key_id=unauthorized_key.key_id,
        effects=(effect,),
        signer=unauthorized_signer,
        idempotency_key="unauthorized-dependency",
    )
    unauthorized_decision = _commit(
        unauthorized_store, unauthorized_genesis, unauthorized_candidate
    )
    assert unauthorized_decision is not None and not unauthorized_decision.accepted
    assert not unauthorized_store.scan_runtime_records(
        record_type="claim.constraint.placed"
    )
    unauthorized_rejected = {
        record.payload["candidate_id"]
        for _, record in unauthorized_store.scan_runtime_records(
            record_type="candidate.rejected"
        )
    }
    assert unauthorized_candidate.id in unauthorized_rejected

    mixed = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        effects=(
            task_dependency_link_effect(left.id, right.id, "first effect"),
            task_dependency_link_effect(right.id, left.id, "second effect"),
        ),
        signer=signer,
        idempotency_key="mixed-dependency-effects",
    )
    mixed_decision = _commit(first, genesis, mixed)
    assert mixed_decision is not None and not mixed_decision.accepted
    assert not first.scan_runtime_records(record_type="claim.constraint.placed")
    rejected_ids = {
        record.payload["candidate_id"]
        for _, record in first.scan_runtime_records(record_type="candidate.rejected")
    }
    assert mixed.id in rejected_ids
    unauthorized_store.close()
    first.close()
    second.close()
