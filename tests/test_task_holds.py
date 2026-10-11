from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.claim_holds import open_claim_holds
from aigineering.core.claim_holds import ClaimHoldConflict, validate_claim_hold_batch
from aigineering.core.commitment import (
    CandidateCommitRejected,
    CandidateCommitter,
    reduce_candidate,
)
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer, create_verifier
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.collaboration import (
    COLLABORATION_EFFECTS,
    project_task_holds,
)
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    task_block_effect,
    task_unblock_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.task_views import project_task_status
from aigineering.task_view_query import load_task_view_query_context


def _setup(tmp_path, *, capabilities=None):
    store = SQLiteStore(db_path=str(tmp_path / "holds.sqlite"))
    signer = Ed25519Signer()
    effective = tuple(
        sorted(
            set(
                capabilities
                or (
                    "contract.publish",
                    "task.block",
                    "task.unblock",
                    "task.comment",
                )
            )
            | {"contract.publish"}
        )
    )
    key = ActorKey(
        "human:operator", "operator-key", signer.kind, signer.signer_id, effective
    )
    genesis = create_genesis_manifest("task-holds-test", (key,), "policy:test")
    initialize_genesis(store, genesis)
    contract = build_control_plane_contract(name="held task", description="task")
    published = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="publish-held-task",
    )
    assert published.accepted
    return store, signer, key, genesis, contract


def _proposal(genesis, signer, key, effect, nonce, *, effects=None, **kwargs):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=tuple(effects) if effects is not None else (effect,),
        signer=signer,
        idempotency_key=nonce,
        **kwargs,
    )


def _commit(store, genesis, proposal):
    committer = CandidateCommitter(
        store, store, additional_effects=COLLABORATION_EFFECTS
    )
    return committer.commit(proposal, genesis)


def _record(decision, record_type):
    return next(
        record
        for record in decision.runtime_records
        if record.record_type == record_type
    )


def test_signed_block_unblock_tracks_multiple_open_holds_and_exact_releases(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    first = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_block_effect(contract.id, "needs input"),
            "block-1",
        ),
    )
    second = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_block_effect(contract.id, "review pending"),
            "block-2",
        ),
    )
    assert first.accepted and second.accepted
    first_hold = _record(first, "claim.hold.placed")
    second_hold = _record(second, "claim.hold.placed")
    assert [
        r.id for r in project_task_holds(store_records(store), contract_id=contract.id)
    ] == [
        first_hold.id,
        second_hold.id,
    ]

    released = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_unblock_effect(contract.id, first_hold.id, "input received"),
            "unblock-1",
        ),
    )
    assert released.accepted
    release_record = _record(released, "claim.hold.released")
    assert release_record.payload["hold_id"] == first_hold.id
    assert first_hold.id in release_record.causal_parents
    assert [
        r.id for r in project_task_holds(store_records(store), contract_id=contract.id)
    ] == [second_hold.id]

    duplicate = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_unblock_effect(contract.id, first_hold.id, "duplicate resolution"),
            "unblock-duplicate",
        ),
    )
    assert not duplicate.accepted
    assert len(store.scan_runtime_records(record_type="claim.hold.released")) == 1


def store_records(store):
    return tuple(record for _, record in store.scan_runtime_records())


def test_exact_candidate_replay_survives_closed_hold_and_terminal(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    block_proposal = _proposal(
        genesis, signer, key, task_block_effect(contract.id, "wait"), "block-replay"
    )
    block = _commit(store, genesis, block_proposal)
    hold = _record(block, "claim.hold.placed")
    unblock_proposal = _proposal(
        genesis,
        signer,
        key,
        task_unblock_effect(contract.id, hold.id, "resolved"),
        "unblock-replay",
    )
    unblock = _commit(store, genesis, unblock_proposal)
    assert unblock.accepted

    replay = _commit(store, genesis, unblock_proposal)
    assert replay.accepted
    assert (
        _record(replay, "claim.hold.released").id
        == _record(unblock, "claim.hold.released").id
    )
    assert len(store.scan_runtime_records(record_type="claim.hold.released")) == 1

    from aigineering.core.lifecycle_facts import create_terminal_record

    store.append_runtime_record(
        create_terminal_record(contract.id, "complete", actor_id="system")
    )
    store.new_entry(contract.id, "complete")
    replay_block = _commit(store, genesis, block_proposal)
    assert replay_block.accepted
    assert _record(replay_block, "claim.hold.placed").id == hold.id
    assert len(store.scan_runtime_records(record_type="claim.hold.placed")) == 1


def test_hold_blocks_direct_and_pull_claims_and_rebuilds_projection(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    blocked = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_block_effect(contract.id, "waiting"),
            "block-claim",
        ),
    )
    assert blocked.accepted
    assert store.claim_contract(contract.id, "worker:direct") is None
    from aigineering.runtime import claim_next_package

    assert (
        claim_next_package(store, worker_id="worker:pull", contract_id=contract.id)
        is None
    )
    view = RuntimeProjection(store, store).contract_view(contract)
    assert not view.enabled
    assert any(item.startswith("claim_hold:") for item in view.blockers)
    legacy_status = project_task_status(contract, store)
    query_context = load_task_view_query_context(store)
    assert (
        project_task_status(
            contract,
            store,
            projection_context=query_context.effect_projection,
            query_context=query_context,
        )
        == legacy_status
    )

    reopened = SQLiteStore(db_path=str(tmp_path / "holds.sqlite"))
    reopened.rebuild_runtime_materializations()
    rebuilt = RuntimeProjection(reopened, reopened).contract_view(contract)
    assert rebuilt.projection_hash == view.projection_hash
    assert project_task_holds(store_records(reopened), contract_id=contract.id)


def test_as_of_revision_projects_placement_and_release_without_history_bleed(
    tmp_path,
):
    store, signer, key, genesis, contract = _setup(tmp_path)
    baseline_revision = max(revision for revision, _ in store.scan_runtime_records())
    baseline = RuntimeProjection(
        store, store, as_of_revision=baseline_revision
    ).contract_view(contract)
    block = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_block_effect(contract.id, "wait"),
            "revision-block",
        ),
    )
    hold = _record(block, "claim.hold.placed")
    placed_revision = max(
        revision
        for revision, record in store.scan_runtime_records()
        if record.id == hold.id
    )
    placed_view = RuntimeProjection(
        store, store, as_of_revision=placed_revision
    ).contract_view(contract)
    assert not placed_view.enabled
    assert any(item == f"claim_hold:{hold.id}" for item in placed_view.blockers)
    assert placed_view.projection_hash != baseline.projection_hash
    assert (
        RuntimeProjection(store, store, as_of_revision=baseline_revision)
        .contract_view(contract)
        .projection_hash
        == baseline.projection_hash
    )

    released = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_unblock_effect(contract.id, hold.id, "ready"),
            "revision-release",
        ),
    )
    release = _record(released, "claim.hold.released")
    released_revision = max(
        revision
        for revision, record in store.scan_runtime_records()
        if record.id == release.id
    )
    released_view = RuntimeProjection(
        store, store, as_of_revision=released_revision
    ).contract_view(contract)
    assert released_view.enabled == baseline.enabled
    assert released_view.blockers == baseline.blockers
    assert released_view.projection_hash != placed_view.projection_hash
    assert (
        RuntimeProjection(store, store, as_of_revision=placed_revision)
        .contract_view(contract)
        .projection_hash
        == placed_view.projection_hash
    )


def test_claim_bound_worker_cannot_place_or_release_holds(tmp_path):
    store, signer, key, genesis, contract = _setup(
        tmp_path,
        capabilities=(
            "contract.publish",
            "worker.submit",
            "task.block",
            "task.unblock",
        ),
    )
    store.register_worker(
        WorkerRegistration(
            key.actor_id,
            actor_id=key.actor_id,
            key_id=key.key_id,
        )
    )
    claim = store.claim_contract(contract.id, key.actor_id, package_id="pkg:worker")
    assert claim is not None
    binding = CandidateClaimBinding(
        contract_id=contract.id,
        claim_id=claim["claim_id"],
        claim_epoch=claim["epoch"],
        package_id=claim["package_id"],
    )
    for effect, nonce in (
        (task_block_effect(contract.id, "worker block"), "worker-block"),
        (
            task_unblock_effect(contract.id, "record:unowned-hold", "worker unblock"),
            "worker-unblock",
        ),
    ):
        proposal = _proposal(
            genesis,
            signer,
            key,
            effect,
            nonce,
            claim_binding=binding,
        )
        decision = reduce_candidate(
            proposal,
            genesis,
            verifier_factory=create_verifier,
            actor_keys=(key,),
            projection_context=load_effect_projection_context(store, actor_keys=(key,)),
            additional_effects=COLLABORATION_EFFECTS,
        )
        assert not decision.accepted
        rejection = next(
            record
            for record in decision.runtime_records
            if record.record_type == "candidate.rejected"
        )
        assert rejection.payload["reason"] == (
            "claim-bound Candidate contains an unsupported effect"
        )
    assert store.get_claim(contract.id)["status"] == "active"
    assert store.scan_runtime_records(record_type="claim.hold.placed") == []
    assert store.scan_runtime_records(record_type="claim.hold.released") == []


def test_another_authorized_actor_can_release_exact_hold_and_owns_provenance(
    tmp_path,
):
    store = SQLiteStore(db_path=str(tmp_path / "multi-actor.sqlite"))
    signer_a, signer_b, signer_c = Ed25519Signer(), Ed25519Signer(), Ed25519Signer()
    key_a = ActorKey(
        "human:one",
        "key:one",
        signer_a.kind,
        signer_a.signer_id,
        ("contract.publish", "task.block"),
    )
    key_b = ActorKey(
        "human:two",
        "key:two",
        signer_b.kind,
        signer_b.signer_id,
        ("task.unblock",),
    )
    key_c = ActorKey(
        "human:three",
        "key:three",
        signer_c.kind,
        signer_c.signer_id,
        (),
    )
    genesis = create_genesis_manifest(
        "multi-actor-hold-test", (key_a, key_b, key_c), "policy:test"
    )
    initialize_genesis(store, genesis)
    contract = build_control_plane_contract(name="multi actor", description="task")
    assert publish_effect(
        store,
        store,
        genesis,
        key_a,
        signer_a,
        contract_declaration_effect(contract),
        idempotency_key="multi-actor-contract",
    ).accepted
    placed = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer_a,
            key_a,
            task_block_effect(contract.id, "one actor blocks"),
            "actor-block",
        ),
    )
    hold = _record(placed, "claim.hold.placed")

    denied = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer_c,
            key_c,
            task_unblock_effect(contract.id, hold.id, "unauthorized"),
            "actor-denied-release",
        ),
    )
    assert not denied.accepted
    released = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer_b,
            key_b,
            task_unblock_effect(contract.id, hold.id, "resolved by reviewer"),
            "actor-authorized-release",
        ),
    )
    assert released.accepted
    release_record = _record(released, "claim.hold.released")
    assert release_record.payload["actor_id"] == key_b.actor_id
    assert release_record.payload["key_id"] == key_b.key_id
    assert release_record.payload["candidate_id"] == released.candidate_id
    assert hold.id in release_record.causal_parents


def test_exact_old_block_replay_after_release_and_claim_does_not_reopen_hold(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    block_proposal = _proposal(
        genesis, signer, key, task_block_effect(contract.id, "wait"), "old-block"
    )
    original = _commit(store, genesis, block_proposal)
    hold = _record(original, "claim.hold.placed")
    release = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_unblock_effect(contract.id, hold.id, "resolved"),
            "old-block-release",
        ),
    )
    assert release.accepted
    claimed = store.claim_contract(contract.id, "worker:after-release")
    assert claimed is not None
    claim_before_replay = store.get_claim(contract.id)

    replay = _commit(store, genesis, block_proposal)

    assert replay.accepted
    assert _record(replay, "claim.hold.placed").id == hold.id
    placed_facts = store.scan_runtime_records(record_type="claim.hold.placed")
    assert len(placed_facts) == 1
    assert placed_facts[0][1].id == hold.id
    assert store.get_claim(contract.id) == claim_before_replay
    assert open_claim_holds(store_records(store), contract_id=contract.id) == ()


def test_block_after_any_claim_history_is_rejected_without_mutating_claim(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    claim = store.claim_contract(contract.id, "worker:claimed")
    assert claim is not None
    before = store.get_claim(contract.id)

    decision = _commit(
        store,
        genesis,
        _proposal(
            genesis, signer, key, task_block_effect(contract.id, "late"), "late-block"
        ),
    )

    assert not decision.accepted
    assert store.get_claim(contract.id) == before
    assert store.scan_runtime_records(record_type="claim.hold.placed") == []


def test_claim_that_wins_after_plugin_snapshot_rejects_block_transaction(
    tmp_path, monkeypatch
):
    store, signer, key, genesis, contract = _setup(tmp_path)
    proposal = _proposal(
        genesis, signer, key, task_block_effect(contract.id, "racing"), "stale-block"
    )
    stale_context = load_effect_projection_context(store)
    import aigineering.core.commitment as commitment
    import aigineering.core.commit_snapshot as commit_snapshot

    monkeypatch.setattr(
        commitment,
        "load_effect_projection_context",
        lambda *_args, **_kwargs: stale_context,
    )
    monkeypatch.setattr(
        commit_snapshot,
        "load_effect_projection_context",
        lambda *_args, **_kwargs: stale_context,
    )
    claim = store.claim_contract(contract.id, "worker:racing")
    assert claim is not None

    with pytest.raises(CandidateCommitRejected):
        _commit(store, genesis, proposal)

    assert store.get_claim(contract.id) == claim
    assert store.scan_runtime_records(record_type="claim.hold.placed") == []
    assert any(
        record.payload.get("candidate_id") == proposal.id
        for _, record in store.scan_runtime_records(record_type="candidate.rejected")
    )


def test_terminal_precedes_hold_and_unblock_can_resolve_terminal_task(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    block_proposal = _proposal(
        genesis,
        signer,
        key,
        task_block_effect(contract.id, "pending"),
        "terminal-block",
    )
    block = _commit(store, genesis, block_proposal)
    hold = _record(block, "claim.hold.placed")
    from aigineering.core.lifecycle_facts import create_terminal_record

    store.append_runtime_record(
        create_terminal_record(contract.id, "complete", actor_id="system")
    )
    store.new_entry(contract.id, "complete")
    terminal_view = RuntimeProjection(store, store).contract_view(contract)
    assert terminal_view.blockers == ("terminal:complete",)
    assert store.claim_contract(contract.id, "worker:terminal") is None

    unblock = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_unblock_effect(contract.id, hold.id, "close audit"),
            "terminal-unblock",
        ),
    )
    assert unblock.accepted
    assert project_task_holds(store_records(store), contract_id=contract.id) == ()

    other = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_block_effect(contract.id, "late"),
            "terminal-new-block",
        ),
    )
    assert not other.accepted


def test_block_unblock_require_authority_exact_payload_and_single_effect(tmp_path):
    store, signer, key, genesis, contract = _setup(
        tmp_path, capabilities=("contract.publish", "task.unblock")
    )
    denied = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_block_effect(contract.id, "unauthorized"),
            "no-cap",
        ),
    )
    assert not denied.accepted

    forged = _proposal(
        genesis,
        signer,
        key,
        task_block_effect(contract.id, "forged"),
        "forged-payload",
        effects=(
            CandidateEffect(
                "task.block",
                {
                    "contract_id": contract.id,
                    "reason": "forged",
                    "actor_id": "human:admin",
                },
            ),
        ),
    )
    assert not _commit(store, genesis, forged).accepted
    assert store.scan_runtime_records(record_type="claim.hold.placed") == []

    full_store, full_signer, full_key, full_genesis, full_contract = _setup(
        tmp_path / "full", capabilities=("task.block", "task.unblock")
    )
    mixed = _proposal(
        full_genesis,
        full_signer,
        full_key,
        task_block_effect(full_contract.id, "mixed"),
        "mixed",
        effects=(
            task_block_effect(full_contract.id, "mixed"),
            task_unblock_effect(full_contract.id, "missing-hold", "mixed"),
        ),
    )
    assert not _commit(full_store, full_genesis, mixed).accepted


def test_claim_hold_facts_require_authenticated_heads_and_supported_schema(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    candidate_id = "candidate:missing-head"
    placement = create_runtime_record(
        "claim.hold.placed",
        {
            "contract_id": contract.id,
            "reason": "forged",
            "actor_id": key.actor_id,
            "key_id": key.key_id,
            "candidate_id": candidate_id,
        },
        causal_parents=("record:receipt",),
    )
    with pytest.raises(ClaimHoldConflict, match="authenticated Candidate head"):
        validate_claim_hold_batch(
            (),
            (placement,),
            candidate_id=candidate_id,
            actor_id=key.actor_id,
            key_id=key.key_id,
        )
    with pytest.raises(ClaimHoldConflict, match="authenticated Candidate commitment"):
        store.append_runtime_record(placement)

    unknown_schema = create_runtime_record(
        "claim.hold.placed",
        {
            "contract_id": contract.id,
            "reason": "future",
            "actor_id": key.actor_id,
            "key_id": key.key_id,
            "candidate_id": candidate_id,
        },
        causal_parents=("record:receipt",),
        schema_version=2,
    )
    with pytest.raises(ClaimHoldConflict, match="schema version"):
        open_claim_holds((unknown_schema,))


def test_concurrent_releases_of_one_hold_accept_exactly_one(tmp_path):
    initial, signer, key, genesis, contract = _setup(tmp_path)
    block = _commit(
        initial,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            task_block_effect(contract.id, "wait"),
            "race-release-block",
        ),
    )
    hold = _record(block, "claim.hold.placed")
    initial.close()
    stores = [
        SQLiteStore(db_path=str(tmp_path / "holds.sqlite")),
        SQLiteStore(db_path=str(tmp_path / "holds.sqlite")),
    ]
    proposals = [
        _proposal(
            genesis,
            signer,
            key,
            task_unblock_effect(contract.id, hold.id, "release race A"),
            "release-race-A",
        ),
        _proposal(
            genesis,
            signer,
            key,
            task_unblock_effect(contract.id, hold.id, "release race B"),
            "release-race-B",
        ),
    ]
    barrier = Barrier(2)
    lock = Lock()
    waited = [False, False]
    for index, store in enumerate(stores):
        original = store.commit_ingress_batch

        def delayed(*args, _index=index, _original=original, **kwargs):
            with lock:
                should_wait = not waited[_index]
                waited[_index] = True
            if should_wait:
                barrier.wait(timeout=10)
            return _original(*args, **kwargs)

        store.commit_ingress_batch = delayed

    def release(index):
        try:
            return _commit(stores[index], genesis, proposals[index]).accepted
        except CandidateCommitRejected:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(release, (0, 1)))
    assert sum(results) == 1
    assert len(stores[0].scan_runtime_records(record_type="claim.hold.released")) == 1
    assert open_claim_holds(store_records(stores[0]), contract_id=contract.id) == ()


def test_block_claim_race_is_serialized_by_two_sqlite_connections(tmp_path):
    initial, signer, key, genesis, contract = _setup(tmp_path)
    initial.close()
    block_store = SQLiteStore(db_path=str(tmp_path / "holds.sqlite"))
    claim_store = SQLiteStore(db_path=str(tmp_path / "holds.sqlite"))
    proposal = _proposal(
        genesis, signer, key, task_block_effect(contract.id, "racing"), "race-block"
    )
    barrier = Barrier(2)
    lock = Lock()
    waited = {"block": False, "claim": False}
    original_commit = block_store.commit_ingress_batch
    original_claim = claim_store.claim_contract

    def delayed_commit(*args, **kwargs):
        with lock:
            should_wait = not waited["block"]
            waited["block"] = True
        if should_wait:
            barrier.wait(timeout=10)
        return original_commit(*args, **kwargs)

    def delayed_claim(*args, **kwargs):
        with lock:
            should_wait = not waited["claim"]
            waited["claim"] = True
        if should_wait:
            barrier.wait(timeout=10)
        return original_claim(*args, **kwargs)

    block_store.commit_ingress_batch = delayed_commit
    claim_store.claim_contract = delayed_claim

    def place():
        try:
            decision = _commit(block_store, genesis, proposal)
            return ("block", decision.accepted)
        except CandidateCommitRejected:
            return ("block", False)

    def claim():
        return ("claim", claim_store.claim_contract(contract.id, "worker:race"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        block_future = pool.submit(place)
        claim_future = pool.submit(claim)
        block_result = block_future.result(timeout=20)
        claim_result = claim_future.result(timeout=20)

    block_won = block_result[1] is True
    claim_won = claim_result[1] is not None
    assert block_won != claim_won
    if block_won:
        assert claim_store.get_claim(contract.id) is None
        assert (
            len(claim_store.scan_runtime_records(record_type="claim.hold.placed")) == 1
        )
    else:
        active = claim_store.get_claim(contract.id)
        assert active is not None
        assert active["status"] == "active"
        assert claim_store.scan_runtime_records(record_type="claim.hold.placed") == []


def test_unblock_claim_race_is_serialized_by_two_sqlite_connections(tmp_path):
    initial, signer, key, genesis, contract = _setup(tmp_path)
    hold_decision = _commit(
        initial,
        genesis,
        _proposal(
            genesis, signer, key, task_block_effect(contract.id, "wait"), "initial-hold"
        ),
    )
    hold = _record(hold_decision, "claim.hold.placed")
    initial.close()
    unblock_store = SQLiteStore(db_path=str(tmp_path / "holds.sqlite"))
    claim_store = SQLiteStore(db_path=str(tmp_path / "holds.sqlite"))
    unblock_proposal = _proposal(
        genesis,
        signer,
        key,
        task_unblock_effect(contract.id, hold.id, "ready"),
        "race-unblock",
    )
    barrier = Barrier(2)
    lock = Lock()
    waited = {"unblock": False, "claim": False}
    original_commit = unblock_store.commit_ingress_batch
    original_claim = claim_store.claim_contract

    def delayed_commit(*args, **kwargs):
        with lock:
            should_wait = not waited["unblock"]
            waited["unblock"] = True
        if should_wait:
            barrier.wait(timeout=10)
        return original_commit(*args, **kwargs)

    def delayed_claim(*args, **kwargs):
        with lock:
            should_wait = not waited["claim"]
            waited["claim"] = True
        if should_wait:
            barrier.wait(timeout=10)
        return original_claim(*args, **kwargs)

    unblock_store.commit_ingress_batch = delayed_commit
    claim_store.claim_contract = delayed_claim

    def release():
        return _commit(unblock_store, genesis, unblock_proposal).accepted

    def claim():
        return claim_store.claim_contract(contract.id, "worker:after-unblock")

    with ThreadPoolExecutor(max_workers=2) as pool:
        release_future = pool.submit(release)
        claim_future = pool.submit(claim)
        released = release_future.result(timeout=20)
        claim_result = claim_future.result(timeout=20)

    assert released
    if claim_result is not None:
        assert claim_store.get_claim(contract.id) == claim_result
        assert claim_result["status"] == "active"
    else:
        assert (
            open_claim_holds(store_records(claim_store), contract_id=contract.id) == ()
        )
