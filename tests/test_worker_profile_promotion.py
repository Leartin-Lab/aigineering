from __future__ import annotations

from threading import Event, Thread
from dataclasses import replace

import pytest

from aigineering.agent.harness import HarnessCandidateAdapter
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.ids import compute_content_hash, hash_contract_v3
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.worker_profile_promotion import (
    WORKER_PROFILE_PROMOTION_EFFECTS,
    validate_worker_profile_promotion,
)
from aigineering.protocol.candidate import (
    ActorKey,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    actor_revocation_effect,
    contract_declaration_effect,
    worker_profile_promotion_effect,
    worker_registration_effect,
)
from aigineering.protocol.candidate import create_candidate_proposal
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package


def _publisher(store):
    signer = Ed25519Signer.from_private_key_hex("31" * 32)
    actor_key = ActorKey(
        "human:profile-owner",
        "profile-owner-root",
        signer.kind,
        signer.signer_id,
        ("actor.authorize", "worker.register"),
    )
    genesis = create_genesis_manifest(
        "worker-profile-promotion-test", (actor_key,), "policy:profile-promotion-v1"
    )
    initialize_genesis(store, genesis)
    return signer, actor_key, genesis


def test_profile_promotion_is_paired_signed_and_exactly_replayable(tmp_path):
    store = SQLiteStore(str(tmp_path / "promotion.db"))
    try:
        root_signer, root_key, genesis = _publisher(store)
        worker_id = "worker:profile-promotion"
        worker_key_id = f"worker-{compute_content_hash(worker_id)[:16]}"
        worker_signer = Ed25519Signer.from_private_key_hex("32" * 32)
        worker_key = ActorKey(
            worker_id,
            worker_key_id,
            worker_signer.kind,
            worker_signer.signer_id,
            ("worker.submit",),
        )
        source = WorkerRegistration(
            worker_id,
            actor_id=worker_id,
            key_id=worker_key_id,
        )
        initial = CandidatePublisher(store, store, genesis, root_key, root_signer)
        registered = initial.publish(
            (
                actor_authorization_effect(worker_key),
                worker_registration_effect(source),
            ),
            idempotency_key="profile-promotion-initial-registration",
        )
        assert registered.accepted
        prior = next(
            record
            for _, record in store.scan_runtime_records(record_type="worker.registered")
            if record.payload.get("worker_id") == worker_id
        )

        promoted = WorkerRegistration(
            worker_id,
            profile_id="openai-compatible-v1",
            version="2",
            actor_id=worker_id,
            key_id=worker_key_id,
        )
        effects = (
            worker_profile_promotion_effect(prior.id, promoted),
            worker_registration_effect(promoted),
        )
        authority = CandidatePublisher(
            store,
            store,
            genesis,
            root_key,
            root_signer,
            additional_effects=WORKER_PROFILE_PROMOTION_EFFECTS,
        )
        first = authority.publish(effects, idempotency_key="promote-profile-v2")
        assert first.accepted
        changed_head = replace(promoted, version="3", enabled=False)
        assert authority.publish(
            (worker_registration_effect(changed_head),),
            idempotency_key="disable-after-profile-promotion",
        ).accepted
        before_replay = tuple(record.id for _, record in store.scan_runtime_records())
        second = authority.publish(effects, idempotency_key="promote-profile-v2")
        after_replay = tuple(record.id for _, record in store.scan_runtime_records())
        assert second.accepted
        assert after_replay == before_replay
        current_after_replay = store.get_worker_registration(worker_id)
        assert current_after_replay is not None
        assert current_after_replay.version == "3"
        assert not current_after_replay.enabled
        digest = store.runtime_materialization_digest()
        assert store.rebuild_runtime_materializations() == digest
        assert store.get_worker_registration(worker_id) == current_after_replay
        distinct_candidate = authority.publish(
            effects, idempotency_key="different-intent-old-promotion"
        )
        assert not distinct_candidate.accepted
        assert store.get_worker_registration(worker_id) == current_after_replay
        assert (
            len(store.scan_runtime_records(record_type="worker.profile.promoted")) == 1
        )

        promotion_records = store.scan_runtime_records(
            record_type="worker.profile.promoted"
        )
        assert len(promotion_records) == 1
        promotion = promotion_records[0][1]
        registration = store.get_worker_registration(worker_id)
        assert registration is not None
        assert registration.version == "3"
        assert not registration.enabled
        assert promotion.payload["actor_id"] == worker_id
        assert promotion.payload["key_id"] == worker_key_id
        assert promotion.causal_parents == (
            next(
                record.id
                for _, record in store.scan_runtime_records(
                    record_type="candidate.received"
                )
                if record.payload.get("candidate_id")
                == promotion.payload["candidate_id"]
            ),
            prior.id,
            next(
                record.id
                for _, record in store.scan_runtime_records(
                    record_type="worker.registered"
                )
                if record.payload.get("version") == "2"
                and record.payload.get("worker_id") == worker_id
            ),
        )
        commit = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.committed"
            )
            if record.payload.get("candidate_id") == promotion.payload["candidate_id"]
        )
        assert len(commit.payload["committed_record_ids"]) == 2
        assert promotion.id in commit.payload["committed_record_ids"]
    finally:
        store.close()


def test_profile_promotion_writer_preflight_refuses_claim_history():
    worker_id = "worker:profile-claimed"
    worker_key_id = f"worker-{compute_content_hash(worker_id)[:16]}"
    worker_signer = Ed25519Signer.from_private_key_hex("33" * 32)
    worker_key = ActorKey(
        worker_id,
        worker_key_id,
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    source = WorkerRegistration(worker_id, actor_id=worker_id, key_id=worker_key_id)
    prior = create_runtime_record(
        "worker.registered",
        {
            "worker_id": worker_id,
            "actor_id": worker_id,
            "key_id": worker_key_id,
            "capabilities": [],
            "pools": [],
            "profile_id": "",
            "capacity": 1,
            "enabled": True,
            "version": "1",
            "registered_by": "human:owner",
        },
        causal_parents=("record:bootstrap",),
    )
    claim = create_runtime_record(
        "claim.granted",
        {
            "claim_id": "claim:historical-active",
            "contract_id": "task:historical-active",
            "worker_id": worker_id,
        },
        causal_parents=("record:claim-receipt",),
    )
    context = EffectProjectionContext(
        runtime_records=(prior, claim),
        actor_keys=(worker_key,),
        registrations=(replace(source, active_claims=1),),
    )
    requested = WorkerRegistration(
        worker_id,
        profile_id="openai-compatible-v1",
        version="2",
        actor_id=worker_id,
        key_id=worker_key_id,
    )
    try:
        validate_worker_profile_promotion(
            context,
            previous_registration_id=prior.id,
            registration=requested,
        )
    except ValueError as exc:
        assert "active claim" in str(exc)
    else:
        raise AssertionError("promotion must be blocked by claim history")

    expired = create_runtime_record(
        "claim.expired",
        {"claim_id": "claim:historical-active"},
        causal_parents=("record:expiry-receipt", claim.id),
    )
    closed_context = EffectProjectionContext(
        runtime_records=(prior, claim, expired),
        actor_keys=(worker_key,),
        registrations=(source,),
    )
    assert (
        validate_worker_profile_promotion(
            closed_context,
            previous_registration_id=prior.id,
            registration=WorkerRegistration(
                worker_id,
                profile_id="openai-compatible-v1",
                version="2",
                actor_id=worker_id,
                key_id=worker_key_id,
            ),
        )
        == prior
    )


def _race_world(database):
    store = SQLiteStore(str(database))
    operator_signer = Ed25519Signer.from_private_key_hex("41" * 32)
    operator = ActorKey(
        "human:profile-race-owner",
        "profile-race-root",
        operator_signer.kind,
        operator_signer.signer_id,
        (
            "actor.authorize",
            "actor.revoke",
            "contract.publish",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest(
        "worker-profile-race", (operator,), "policy:profile-race-v1"
    )
    initialize_genesis(store, genesis)
    publisher = CandidatePublisher(store, store, genesis, operator, operator_signer)
    worker_id = "worker:profile-race"
    worker_key_id = f"worker-{compute_content_hash(worker_id)[:16]}"
    worker_signer = Ed25519Signer.from_private_key_hex("42" * 32)
    worker_key = ActorKey(
        worker_id,
        worker_key_id,
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    source = WorkerRegistration(worker_id, actor_id=worker_id, key_id=worker_key_id)
    registration_decision = publisher.publish(
        (
            actor_authorization_effect(worker_key),
            worker_registration_effect(source),
        ),
        idempotency_key="race-worker-registration",
    )
    assert registration_decision.accepted
    fields = {
        "name": "profile promotion race target",
        "description": "Target for the two-connection promotion race.",
        "inputs": (),
        "outputs": ("result",),
        "activation": "",
        "budget": 2,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    assert publisher.publish(
        (contract_declaration_effect(contract),),
        idempotency_key="race-worker-contract",
    ).accepted
    prior = next(
        record
        for _, record in store.scan_runtime_records(record_type="worker.registered")
        if record.payload.get("worker_id") == worker_id
    )
    promoted = WorkerRegistration(
        worker_id,
        profile_id="openai-compatible-v1",
        version="2",
        actor_id=worker_id,
        key_id=worker_key_id,
    )
    effects = (
        worker_profile_promotion_effect(prior.id, promoted),
        worker_registration_effect(promoted),
    )
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=operator.actor_id,
        key_id=operator.key_id,
        effects=effects,
        signer=operator_signer,
        idempotency_key="race-profile-promotion",
    )
    committer = CandidateCommitter(
        store,
        store,
        additional_effects=WORKER_PROFILE_PROMOTION_EFFECTS,
    )
    adapter = HarnessCandidateAdapter(genesis.id, worker_key, worker_signer)
    return (
        store,
        publisher,
        genesis,
        worker_key,
        adapter,
        contract,
        candidate,
        committer,
    )


def test_claim_wins_between_profile_projection_and_sqlite_writer(tmp_path):
    database = tmp_path / "claim-wins-profile-promotion.db"
    store, _publisher, genesis, worker_key, adapter, contract, candidate, committer = (
        _race_world(database)
    )
    claimant = SQLiteStore(str(database))
    reached_writer = Event()
    release_writer = Event()
    original_commit = store.commit_ingress_batch
    outcomes = []
    errors = []

    def pause_before_writer(*args, **kwargs):
        if not reached_writer.is_set() and any(
            record.record_type == "worker.profile.promoted"
            for record in kwargs.get("runtime_records", ())
        ):
            reached_writer.set()
            assert release_writer.wait(10), "promotion writer race timed out"
        return original_commit(*args, **kwargs)

    store.commit_ingress_batch = pause_before_writer

    def promote():
        try:
            outcomes.append(committer.commit(candidate, genesis))
        except Exception as exc:  # make thread failures visible to the assertion
            errors.append(exc)

    thread = Thread(target=promote)
    thread.start()
    try:
        assert reached_writer.wait(10), "promotion did not reach its writer gate"
        claim = claim_next_package(
            claimant,
            worker_id=worker_key.actor_id,
            contract_id=contract.id,
            claim_candidate=adapter.claim_candidate(
                request_id="claim-before-profile-promotion",
                contract_id=contract.id,
            ),
            expected_registration_version="1",
        )
        assert claim is not None
        release_writer.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not errors
        assert len(outcomes) == 1 and not outcomes[0].accepted
        assert claimant.get_claim(contract.id)["status"] == "active"
        assert not store.scan_runtime_records(record_type="worker.profile.promoted")
        assert len(store.scan_runtime_records(record_type="candidate.rejected")) == 1
        assert store.get_worker_registration(worker_key.actor_id).version == "1"
    finally:
        release_writer.set()
        thread.join(timeout=10)
        claimant.close()
        store.close()


def test_promotion_wins_between_claim_selection_and_sqlite_writer(tmp_path):
    database = tmp_path / "promotion-wins-claim.db"
    store, _publisher, genesis, worker_key, adapter, contract, candidate, committer = (
        _race_world(database)
    )
    promoter = SQLiteStore(str(database))
    reached_claim_writer = Event()
    release_claim_writer = Event()
    original_claim = store.claim_contract
    claim_errors = []
    claim_results = []

    def pause_claim_writer(*args, **kwargs):
        reached_claim_writer.set()
        assert release_claim_writer.wait(10), "claim writer race timed out"
        return original_claim(*args, **kwargs)

    store.claim_contract = pause_claim_writer

    def select_and_claim():
        try:
            claim_results.append(
                claim_next_package(
                    store,
                    worker_id=worker_key.actor_id,
                    contract_id=contract.id,
                    claim_candidate=adapter.claim_candidate(
                        request_id="claim-selected-before-profile-promotion",
                        contract_id=contract.id,
                    ),
                    expected_registration_version="1",
                )
            )
        except Exception as exc:
            claim_errors.append(exc)

    thread = Thread(target=select_and_claim)
    thread.start()
    try:
        assert reached_claim_writer.wait(10), "claim did not reach its writer gate"
        decision = CandidateCommitter(
            promoter,
            promoter,
            additional_effects=WORKER_PROFILE_PROMOTION_EFFECTS,
        ).commit(candidate, genesis)
        assert decision.accepted
        release_claim_writer.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert claim_results == []
        assert len(claim_errors) == 1
        assert isinstance(claim_errors[0], ValueError)
        assert "worker registration changed after package selection" in str(
            claim_errors[0]
        )
        assert store.get_claim(contract.id) is None
        assert store.get_worker_registration(worker_key.actor_id).version == "2"
        assert (
            len(store.scan_runtime_records(record_type="worker.profile.promoted")) == 1
        )
        assert not store.scan_runtime_records(record_type="claim.granted")
        assert not store.scan_runtime_records(record_type="worker.claim.requested")
    finally:
        release_claim_writer.set()
        thread.join(timeout=10)
        promoter.close()
        store.close()


@pytest.mark.parametrize("mutation", ["disable", "revoke-key"])
def test_promotion_rechecks_registration_head_and_key_at_writer(tmp_path, mutation):
    database = tmp_path / f"promotion-writer-{mutation}.db"
    (
        store,
        _publisher,
        genesis,
        worker_key,
        _adapter,
        _contract,
        candidate,
        committer,
    ) = _race_world(database)
    competitor = SQLiteStore(str(database))
    original_commit = store.commit_ingress_batch
    injected = False

    def mutate_between_projection_and_writer(*args, **kwargs):
        nonlocal injected
        if not injected and any(
            record.record_type == "worker.profile.promoted"
            for record in kwargs.get("runtime_records", ())
        ):
            injected = True
            authority = CandidatePublisher(
                competitor,
                competitor,
                genesis,
                _publisher.actor_key,
                _publisher.signer,
            )
            if mutation == "disable":
                current = competitor.get_worker_registration(worker_key.actor_id)
                assert current is not None
                changed = replace(current, enabled=False, version="2")
                result = authority.publish(
                    (worker_registration_effect(changed),),
                    idempotency_key="concurrent-disable-registration",
                )
            else:
                result = authority.publish(
                    (
                        actor_revocation_effect(
                            worker_key.actor_id,
                            worker_key.key_id,
                            "revoke during profile promotion",
                        ),
                    ),
                    idempotency_key="concurrent-revoke-worker-key",
                )
            assert result.accepted
        return original_commit(*args, **kwargs)

    store.commit_ingress_batch = mutate_between_projection_and_writer
    try:
        decision = committer.commit(candidate, genesis)
        assert injected
        assert not decision.accepted
        assert not store.scan_runtime_records(record_type="worker.profile.promoted")
        assert len(store.scan_runtime_records(record_type="candidate.rejected")) == 1
        if mutation == "disable":
            current = store.get_worker_registration(worker_key.actor_id)
            assert (
                current is not None and not current.enabled and current.version == "2"
            )
        else:
            assert any(
                record.payload.get("actor_id") == worker_key.actor_id
                and record.payload.get("key_id") == worker_key.key_id
                for _, record in store.scan_runtime_records(record_type="actor.revoked")
            )
    finally:
        competitor.close()
        store.close()


def test_elapsed_but_still_active_sqlite_claim_blocks_promotion(tmp_path):
    (
        store,
        _publisher,
        _genesis,
        worker_key,
        adapter,
        contract,
        _candidate,
        _committer,
    ) = _race_world(tmp_path / "elapsed-active-claim.db")
    try:
        claim = claim_next_package(
            store,
            worker_id=worker_key.actor_id,
            contract_id=contract.id,
            claim_candidate=adapter.claim_candidate(
                request_id="elapsed-active-profile-claim", contract_id=contract.id
            ),
            expected_registration_version="1",
        )
        assert claim is not None
        with store._conn:
            store._conn.execute(
                "UPDATE worker_claims SET lease_until = ? WHERE claim_id = ?",
                ("2000-01-01T00:00:00+00:00", claim.package.claim_id),
            )
        current = store.get_worker_registration(worker_key.actor_id)
        assert current is not None and current.active_claims == 1
        genesis = load_genesis(store)
        context = load_effect_projection_context(
            store,
            actor_keys=load_effective_actor_keys(store, genesis),
        )
        prior = next(
            record
            for _, record in store.scan_runtime_records(record_type="worker.registered")
            if record.payload.get("worker_id") == worker_key.actor_id
        )
        with pytest.raises(ValueError, match="active claim"):
            validate_worker_profile_promotion(
                context,
                previous_registration_id=prior.id,
                registration=WorkerRegistration(
                    worker_key.actor_id,
                    profile_id="openai-compatible-v1",
                    version="2",
                    actor_id=worker_key.actor_id,
                    key_id=worker_key.key_id,
                ),
            )
    finally:
        store.close()
