from __future__ import annotations

from types import MappingProxyType

import pytest

from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.commitment import CandidateCommitRejected, CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.runtime_record import create_runtime_record


def _batch_projector(effect, candidate, receipt_id, context):
    records = []
    for item in effect.payload["transitions"]:
        record_type = item["record_type"]
        payload = {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": item["contract_id"],
            "key_id": candidate.key_id,
            "reason": item["reason"],
        }
        parents = (receipt_id,)
        if record_type == "claim.hold.released":
            payload["hold_id"] = item["hold_id"]
            parents = (receipt_id, item["hold_id"])
        record = create_runtime_record(
            record_type,
            payload,
            causal_parents=parents,
        )
        if item.get("duplicate_record"):
            records.append(record)
        records.append(record)
    target = effect.payload["transitions"][0]["contract_id"]
    return EffectProjection(tuple(records), target)


_BATCH_EFFECTS = MappingProxyType(
    {
        "test.claim-hold.batch": EffectHandler(
            capability="test.claim-hold.batch",
            projector=_batch_projector,
            emitted_fact_types=frozenset({"claim.hold.placed", "claim.hold.released"}),
            direct_output_fact_types=frozenset(
                {"claim.hold.placed", "claim.hold.released"}
            ),
        )
    }
)


def _setup(tmp_path):
    db_path = tmp_path / "claim-hold-batches.sqlite"
    store = SQLiteStore(db_path=str(db_path))
    signer = Ed25519Signer()
    key = ActorKey(
        "human:hold-batch-operator",
        "hold-batch-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "test.claim-hold.batch", "task.block"),
    )
    genesis = create_genesis_manifest("claim-hold-batches", (key,), "policy:test")
    initialize_genesis(store, genesis)
    contract = build_control_plane_contract(
        name="batch target", description="hold batch test"
    )
    declaration = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="declare-batch-target",
    )
    assert declaration.accepted
    return store, signer, key, genesis, contract, db_path


def _transition(record_type, contract_id, reason, *, hold_id="", duplicate=False):
    item = {"record_type": record_type, "contract_id": contract_id, "reason": reason}
    if hold_id:
        item["hold_id"] = hold_id
    if duplicate:
        item["duplicate_record"] = True
    return item


def _proposal(genesis, signer, key, transitions, nonce):
    effect = CandidateEffect("test.claim-hold.batch", {"transitions": transitions})
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(effect,),
        signer=signer,
        idempotency_key=nonce,
    )


def _commit(store, genesis, proposal):
    return CandidateCommitter(store, store, additional_effects=_BATCH_EFFECTS).commit(
        proposal, genesis
    )


def _record(decision, record_type):
    return next(
        record
        for record in decision.runtime_records
        if record.record_type == record_type
    )


def _store_records(store):
    return tuple(record for _, record in store.scan_runtime_records())


def test_signed_hold_batch_releases_atomically_and_replays_after_reopen(tmp_path):
    store, signer, key, genesis, contract, db_path = _setup(tmp_path)
    placed = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            (
                _transition("claim.hold.placed", contract.id, "need source A"),
                _transition("claim.hold.placed", contract.id, "need source B"),
            ),
            "place-two-holds",
        ),
    )
    assert placed.accepted
    placement_ids = tuple(
        record.id
        for record in placed.runtime_records
        if record.record_type == "claim.hold.placed"
    )
    assert len(placement_ids) == 2

    release = _proposal(
        genesis,
        signer,
        key,
        tuple(
            _transition(
                "claim.hold.released",
                contract.id,
                f"resolved {index}",
                hold_id=hold_id,
            )
            for index, hold_id in enumerate(placement_ids)
        ),
        "release-two-holds",
    )
    decision = _commit(store, genesis, release)
    assert decision.accepted
    release_records = [
        record
        for record in decision.runtime_records
        if record.record_type == "claim.hold.released"
    ]
    assert {record.payload["hold_id"] for record in release_records} == set(
        placement_ids
    )
    assert all(
        record.payload["contract_id"] == contract.id for record in release_records
    )

    replay = _commit(store, genesis, release)
    assert replay.accepted
    assert [record.id for record in replay.runtime_records] == [
        record.id for record in decision.runtime_records
    ]
    assert len(store.scan_runtime_records(record_type="claim.hold.released")) == 2
    store.close()

    reopened = SQLiteStore(db_path=str(db_path))
    assert reopened.scan_runtime_records(record_type="claim.hold.released")
    from aigineering.core.claim_holds import open_claim_holds

    assert open_claim_holds(_store_records(reopened), contract_id=contract.id) == ()
    reopened.close()


@pytest.mark.parametrize(
    ("transitions", "message"),
    [
        (
            (
                _transition("claim.hold.placed", "target-a", "first"),
                _transition("claim.hold.placed", "target-b", "second"),
            ),
            "one Contract",
        ),
        (
            (
                _transition("claim.hold.placed", "target-a", "first"),
                _transition(
                    "claim.hold.released",
                    "target-a",
                    "second",
                    hold_id="record:hold",
                ),
            ),
            "cannot mix",
        ),
        (
            (_transition("claim.hold.placed", "target-a", "same", duplicate=True),),
            "repeats a record ID",
        ),
        (
            (
                _transition(
                    "claim.hold.released",
                    "target-a",
                    "first",
                    hold_id="record:same-hold",
                ),
                _transition(
                    "claim.hold.released",
                    "target-a",
                    "second",
                    hold_id="record:same-hold",
                ),
            ),
            "repeats a released hold ID",
        ),
    ],
)
def test_malformed_hold_batches_are_durable_rejections_without_partial_facts(
    tmp_path, transitions, message
):
    store, signer, key, genesis, contract, _db_path = _setup(tmp_path)
    normalized = tuple(
        {
            **item,
            "contract_id": (
                contract.id
                if item["contract_id"] == "target-a"
                else item["contract_id"]
            ),
        }
        for item in transitions
    )
    proposal = _proposal(genesis, signer, key, normalized, "malformed-batch")
    with pytest.raises(CandidateCommitRejected, match=message):
        _commit(store, genesis, proposal)
    assert store.scan_runtime_records(record_type="claim.hold.placed") == []
    assert store.scan_runtime_records(record_type="claim.hold.released") == []
    assert len(store.scan_runtime_records(record_type="candidate.rejected")) == 1
    store.close()


def test_unknown_hold_in_batch_rejects_valid_sibling_release_atomically(tmp_path):
    store, signer, key, genesis, contract, _db_path = _setup(tmp_path)
    initial = _commit(
        store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            (
                _transition("claim.hold.placed", contract.id, "first"),
                _transition("claim.hold.placed", contract.id, "second"),
            ),
            "initial-two-holds",
        ),
    )
    hold_ids = tuple(
        record.id
        for record in initial.runtime_records
        if record.record_type == "claim.hold.placed"
    )
    release = _proposal(
        genesis,
        signer,
        key,
        (
            _transition(
                "claim.hold.released", contract.id, "valid", hold_id=hold_ids[0]
            ),
            _transition(
                "claim.hold.released",
                contract.id,
                "missing",
                hold_id="record:unknown-hold",
            ),
        ),
        "release-valid-and-unknown",
    )
    with pytest.raises(CandidateCommitRejected, match="open hold"):
        _commit(store, genesis, release)
    assert store.scan_runtime_records(record_type="claim.hold.released") == []
    from aigineering.core.claim_holds import open_claim_holds

    assert {item.id for item in open_claim_holds(_store_records(store))} == set(
        hold_ids
    )
    store.close()


def test_hold_batch_limit_is_64_transitions(tmp_path):
    store, signer, key, genesis, contract, _db_path = _setup(tmp_path)
    proposal = _proposal(
        genesis,
        signer,
        key,
        tuple(
            _transition("claim.hold.placed", contract.id, f"reason {index}")
            for index in range(65)
        ),
        "too-many-holds",
    )
    with pytest.raises(CandidateCommitRejected, match="exceeds 64"):
        _commit(store, genesis, proposal)
    assert store.scan_runtime_records(record_type="claim.hold.placed") == []
    store.close()


def test_two_connections_cannot_partially_release_the_same_hold_batch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    first_store, signer, key, genesis, contract, db_path = _setup(tmp_path)
    placed = _commit(
        first_store,
        genesis,
        _proposal(
            genesis,
            signer,
            key,
            (
                _transition("claim.hold.placed", contract.id, "one"),
                _transition("claim.hold.placed", contract.id, "two"),
            ),
            "race-place",
        ),
    )
    hold_ids = tuple(
        record.id
        for record in placed.runtime_records
        if record.record_type == "claim.hold.placed"
    )
    stores = [
        first_store,
        SQLiteStore(db_path=str(db_path)),
    ]
    proposals = [
        _proposal(
            genesis,
            signer,
            key,
            tuple(
                _transition(
                    "claim.hold.released",
                    contract.id,
                    f"release by contender {index}",
                    hold_id=hold_id,
                )
                for hold_id in hold_ids
            ),
            f"race-release-{index}",
        )
        for index in range(2)
    ]
    barrier = Barrier(2)

    def attempt(index):
        barrier.wait(timeout=5)
        try:
            return _commit(stores[index], genesis, proposals[index]).accepted
        except CandidateCommitRejected:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert sorted(outcomes) == [False, True]
    release_records = first_store.scan_runtime_records(
        record_type="claim.hold.released"
    )
    assert len(release_records) == len(hold_ids)
    assert {record.payload["hold_id"] for _, record in release_records} == set(hold_ids)
    from aigineering.core.claim_holds import open_claim_holds

    assert open_claim_holds(_store_records(first_store), contract_id=contract.id) == ()
    stores[1].close()
