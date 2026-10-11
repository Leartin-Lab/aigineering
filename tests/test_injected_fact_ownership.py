from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.core.commitment import CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.effect_projection import (
    EffectHandler,
    freeze_additional_effects,
    project_effect_batch,
)
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.injected_fact_replay import exact_single_effect_head_replay
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_candidate_proposal,
    candidate_received_record,
    create_genesis_manifest,
)
from aigineering.protocol.runtime_record import create_runtime_record


PRIORITY_FACT = "task.priority.changed"


def _emitter(record_type: str, *, effect_name: str = "custom.emit"):
    def emit(effect, candidate, receipt_id, _context):
        return EffectProjection(
            (
                create_runtime_record(
                    record_type,
                    {
                        "candidate_id": candidate.id,
                        "effect_type": effect.effect_type,
                    },
                    causal_parents=(receipt_id,),
                ),
            ),
            relation_target=effect_name,
        )

    return emit


def _candidate(genesis, signer, key, *effects, keypart="ownership"):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=effects,
        signer=signer,
        idempotency_key=keypart,
    )


def _env(tmp_path, *, capabilities=("custom.emit", "task.priority.set")):
    store = SQLiteStore(tmp_path / "ownership.sqlite")
    signer = Ed25519Signer()
    key = ActorKey(
        "human:fact-owner-test",
        "fact-owner-key",
        signer.kind,
        signer.signer_id,
        tuple(capabilities),
    )
    genesis = create_genesis_manifest(
        "injected-fact-ownership-test", (key,), "policy:test"
    )
    initialize_genesis(store, genesis)
    return store, signer, key, genesis


def test_explicit_direct_output_set_rejects_priority_fact_and_persists_rejection(
    tmp_path,
):
    store, signer, key, genesis = _env(tmp_path)
    proposal = _candidate(
        genesis,
        signer,
        key,
        CandidateEffect("custom.emit", {}),
    )
    effects = {
        "custom.emit": EffectHandler(
            "custom.emit",
            _emitter(PRIORITY_FACT),
            direct_output_fact_types=frozenset({"custom.fact"}),
        )
    }

    decision = CandidateCommitter(store, store, additional_effects=effects).commit(
        proposal, genesis
    )

    assert not decision.accepted
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    assert "undeclared direct fact type" in rejection.payload["reason"]
    assert store.scan_runtime_records(record_type=PRIORITY_FACT) == []
    assert store.scan_runtime_records(record_type="candidate.rejected")
    store.close()


def test_legacy_typed_handler_without_opt_in_keeps_trusted_compatibility(tmp_path):
    store, signer, key, genesis = _env(tmp_path)
    proposal = _candidate(
        genesis,
        signer,
        key,
        CandidateEffect("custom.emit", {}),
    )
    # The old field retains its historical assignment-admission meaning.
    effects = {
        "custom.emit": EffectHandler(
            "custom.emit",
            _emitter(PRIORITY_FACT),
            emitted_fact_types=frozenset({"custom.fact"}),
        )
    }

    decision = CandidateCommitter(store, store, additional_effects=effects).commit(
        proposal, genesis
    )

    assert decision.accepted
    assert len(store.scan_runtime_records(record_type=PRIORITY_FACT)) == 1
    store.close()


def test_registered_reserved_fact_blocks_legacy_and_other_strict_effects():
    owner = EffectHandler(
        "task.priority.manage",
        _emitter(PRIORITY_FACT, effect_name="priority"),
        direct_output_fact_types=frozenset({PRIORITY_FACT}),
        reserved_fact_types=frozenset({PRIORITY_FACT}),
    )
    strict_emitter = EffectHandler(
        "custom.emit",
        _emitter(PRIORITY_FACT),
        direct_output_fact_types=frozenset({PRIORITY_FACT}),
    )
    legacy_emitter = ("custom.emit", _emitter(PRIORITY_FACT))
    context = EffectProjectionContext()

    for other_effects in (
        {"custom.emit": strict_emitter},
        {"custom.emit": legacy_emitter},
    ):
        candidate = _proposal("task.priority.set", "custom.emit")
        with pytest.raises(ValueError, match="emitted reserved fact type"):
            project_effect_batch(
                candidate,
                "receipt:reserved-owner",
                ("task.priority.manage", "custom.emit"),
                context,
                additional_effects={"task.priority.set": owner, **other_effects},
            )


@pytest.mark.parametrize("legacy", (False, True), ids=("typed", "legacy"))
def test_task_promoted_reserved_fact_blocks_signed_nonowner_publication(
    tmp_path, legacy
):
    store, signer, key, genesis = _env(tmp_path)
    candidate = _candidate(
        genesis,
        signer,
        key,
        CandidateEffect("custom.emit", {}),
        keypart=f"reserved-task-promoted-{legacy}",
    )
    nonowner = (
        ("custom.emit", _emitter("task.promoted"))
        if legacy
        else EffectHandler(
            "custom.emit",
            _emitter("task.promoted"),
            direct_output_fact_types=frozenset({"task.promoted"}),
        )
    )
    effects = {**COLLABORATION_EFFECTS, "custom.emit": nonowner}

    decision = CandidateCommitter(store, store, additional_effects=effects).commit(
        candidate, genesis
    )

    assert not decision.accepted
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    assert "emitted reserved fact type 'task.promoted'" in rejection.payload["reason"]
    assert store.scan_runtime_records(record_type="task.promoted") == []
    assert store.scan_runtime_records(record_type="candidate.committed") == []
    assert store.scan_runtime_records(record_type="candidate.rejected")
    assert any(
        record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
        for _revision, record in store.scan_runtime_records()
    )
    store.close()


def test_reserved_fact_owner_can_emit_its_declared_fact():
    owner = EffectHandler(
        "task.priority.manage",
        _emitter(PRIORITY_FACT),
        direct_output_fact_types=frozenset({PRIORITY_FACT}),
        reserved_fact_types=frozenset({PRIORITY_FACT}),
    )
    candidate = _proposal("task.priority.set")

    projection = project_effect_batch(
        candidate,
        "receipt:reserved-owner",
        ("task.priority.manage",),
        EffectProjectionContext(),
        additional_effects={"task.priority.set": owner},
    )

    assert [record.record_type for record in projection.records] == [PRIORITY_FACT]


def test_duplicate_reserved_owners_are_rejected_when_mapping_is_frozen():
    first = EffectHandler(
        "first",
        _emitter(PRIORITY_FACT),
        direct_output_fact_types=frozenset({PRIORITY_FACT}),
        reserved_fact_types=frozenset({PRIORITY_FACT}),
    )
    second = EffectHandler(
        "second",
        _emitter(PRIORITY_FACT),
        direct_output_fact_types=frozenset({PRIORITY_FACT}),
        reserved_fact_types=frozenset({PRIORITY_FACT}),
    )

    with pytest.raises(ValueError, match="multiple effect owners"):
        freeze_additional_effects({"first": first, "second": second})


def test_strict_empty_set_and_descriptor_validation():
    with pytest.raises(ValueError, match="undeclared direct fact type"):
        project_effect_batch(
            _proposal("custom.emit"),
            "receipt:empty",
            ("custom.emit",),
            EffectProjectionContext(),
            additional_effects={
                "custom.emit": EffectHandler(
                    "custom.emit",
                    _emitter("custom.fact"),
                    direct_output_fact_types=frozenset(),
                )
            },
        )

    for values in (frozenset({""}), frozenset({"   "}), frozenset({1})):
        with pytest.raises(ValueError, match="direct_output_fact_types entries"):
            EffectHandler(
                "custom.emit", _emitter("custom.fact"), direct_output_fact_types=values
            )
    with pytest.raises(ValueError, match="reserved_fact_types requires strict"):
        EffectHandler(
            "custom.emit",
            _emitter("custom.fact"),
            reserved_fact_types=frozenset({"custom.fact"}),
        )
    with pytest.raises(ValueError, match="must be a subset"):
        EffectHandler(
            "custom.emit",
            _emitter("custom.fact"),
            direct_output_fact_types=frozenset({"custom.fact"}),
            reserved_fact_types=frozenset({PRIORITY_FACT}),
        )


def test_handler_sets_and_mapping_are_copied_and_frozen():
    direct = {"custom.fact"}
    reserved = set()
    descriptor = EffectHandler(
        "custom.emit",
        _emitter("custom.fact"),
        direct_output_fact_types=direct,
        reserved_fact_types=reserved,
    )
    original = {"custom.emit": descriptor}
    frozen = freeze_additional_effects(original)
    direct.add(PRIORITY_FACT)
    original.clear()

    assert frozen is not None
    assert frozen["custom.emit"].direct_output_fact_types == frozenset({"custom.fact"})
    assert frozen["custom.emit"].reserved_fact_types == frozenset()
    with pytest.raises(TypeError):
        frozen["another.emit"] = descriptor


def _proposal(*effect_types):
    return type(
        "Proposal",
        (),
        {
            "id": "candidate:test",
            "effects": tuple(CandidateEffect(name, {}) for name in effect_types),
            "claim_binding": None,
        },
    )()


def _head_context(tmp_path):
    store, signer, key, genesis = _env(tmp_path)
    candidate = _candidate(
        genesis, signer, key, CandidateEffect("custom.emit", {}), keypart="replay"
    )
    receipt = candidate_received_record(candidate, genesis)
    direct = create_runtime_record(
        "custom.fact",
        {"candidate_id": candidate.id, "effect_type": "custom.emit"},
        causal_parents=(receipt.id,),
    )
    generated = create_runtime_record(
        "allowance.granted",
        {"candidate_id": candidate.id, "amount": 1},
        causal_parents=(receipt.id,),
    )
    head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [direct.id, generated.id],
        },
        causal_parents=(receipt.id, direct.id, generated.id),
    )
    context = EffectProjectionContext(
        runtime_records=(receipt, direct, generated, head)
    )
    store.close()
    return candidate, receipt, direct, generated, head, context


def test_strict_handler_allows_only_exact_full_head_core_coeffects(tmp_path):
    candidate, receipt, direct, generated, head, context = _head_context(tmp_path)

    def replay(effect, _candidate, _receipt_id, _context):
        return EffectProjection((direct, generated), relation_target="replay")

    projected = project_effect_batch(
        candidate,
        receipt.id,
        ("custom.emit",),
        context,
        additional_effects={
            "custom.emit": EffectHandler(
                "custom.emit",
                replay,
                direct_output_fact_types=frozenset({"custom.fact"}),
            )
        },
    )
    assert projected.records == (direct, generated)

    assert exact_single_effect_head_replay(
        candidate, receipt.id, (direct, generated), context.runtime_records
    )
    # Any change to order or member identity removes the narrow exception.
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, (generated, direct), context.runtime_records
    )
    assert head.id not in {record.id for record in projected.records}


def test_multi_effect_head_replay_gets_no_unlisted_coeffect_exception(tmp_path):
    store, signer, key, genesis = _env(tmp_path)
    candidate = _candidate(
        genesis,
        signer,
        key,
        CandidateEffect("custom.emit", {}),
        CandidateEffect("custom.other", {}),
        keypart="multi-replay",
    )
    receipt = candidate_received_record(candidate, genesis)
    direct = create_runtime_record(
        "custom.fact",
        {"candidate_id": candidate.id, "effect_type": "custom.emit"},
        causal_parents=(receipt.id,),
    )
    generated = create_runtime_record(
        "allowance.granted",
        {"candidate_id": candidate.id, "amount": 1},
        causal_parents=(receipt.id,),
    )
    head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [direct.id, generated.id],
        },
        causal_parents=(receipt.id, direct.id, generated.id),
    )
    context = EffectProjectionContext(
        runtime_records=(receipt, direct, generated, head)
    )

    def direct_replay(effect, _candidate, _receipt_id, _context):
        return EffectProjection((direct, generated), relation_target=effect.effect_type)

    handlers = {
        "custom.emit": EffectHandler(
            "custom.emit",
            direct_replay,
            direct_output_fact_types=frozenset({"custom.fact"}),
        ),
        "custom.other": EffectHandler(
            "custom.other",
            _emitter("custom.other.fact", effect_name="other"),
            direct_output_fact_types=frozenset({"custom.other.fact"}),
        ),
    }
    # Even a head with exact one-member equality is insufficient when the
    # current Candidate itself has multiple effects.
    with pytest.raises(ValueError, match="undeclared direct fact type"):
        project_effect_batch(
            candidate,
            receipt.id,
            ("custom.emit", "custom.other"),
            context,
            additional_effects=handlers,
        )
    store.close()


@pytest.mark.parametrize(
    "tamper", ["duplicate_receipt", "duplicate_member", "foreign_member"]
)
def test_full_head_replay_requires_unique_exact_receipt_and_members(tmp_path, tamper):
    candidate, receipt, direct, generated, head, context = _head_context(tmp_path)
    records = list(context.runtime_records)
    if tamper == "duplicate_receipt":
        records.append(receipt)
    elif tamper == "duplicate_member":
        duplicate_head = create_runtime_record(
            "candidate.committed",
            {
                "candidate_id": candidate.id,
                "committed_record_ids": [direct.id, generated.id],
            },
            causal_parents=(receipt.id, direct.id, generated.id),
        )
        records.append(duplicate_head)
    else:
        foreign = create_runtime_record(
            "custom.fact",
            {"candidate_id": candidate.id, "effect_type": "foreign"},
            causal_parents=(receipt.id,),
        )
        records.append(foreign)
        records[-2] = create_runtime_record(
            "candidate.committed",
            {
                "candidate_id": candidate.id,
                "committed_record_ids": [direct.id, generated.id, foreign.id],
            },
            causal_parents=(receipt.id, direct.id, generated.id, foreign.id),
        )
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, (direct, generated), tuple(records)
    )


def test_full_head_replay_cannot_bypass_another_effects_reserved_fact(tmp_path):
    candidate, receipt, direct, generated, _head, context = _head_context(tmp_path)
    reserved = create_runtime_record(
        PRIORITY_FACT,
        {"candidate_id": candidate.id, "effect_type": "custom.emit"},
        causal_parents=(receipt.id,),
    )
    # Rebuild the valid historical full head with an exclusive fact as an
    # unlisted member, just as a trusted old handler could have emitted it.
    head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [direct.id, reserved.id, generated.id],
        },
        causal_parents=(receipt.id, direct.id, reserved.id, generated.id),
    )
    context = EffectProjectionContext(
        runtime_records=(receipt, direct, reserved, generated, head)
    )

    def replay(effect, _candidate, _receipt_id, _context):
        return EffectProjection((direct, reserved, generated), relation_target="replay")

    effects = {
        "custom.emit": EffectHandler(
            "custom.emit",
            replay,
            direct_output_fact_types=frozenset({"custom.fact"}),
        ),
        "task.priority.set": EffectHandler(
            "task.priority.manage",
            _emitter(PRIORITY_FACT),
            direct_output_fact_types=frozenset({PRIORITY_FACT}),
            reserved_fact_types=frozenset({PRIORITY_FACT}),
        ),
    }
    with pytest.raises(ValueError, match="emitted reserved fact type"):
        project_effect_batch(
            candidate,
            receipt.id,
            ("custom.emit", "task.priority.manage"),
            context,
            additional_effects=effects,
        )


def test_direct_allowlist_does_not_cover_core_generated_coeffects(tmp_path):
    store, signer, key, genesis = _env(tmp_path)
    proposal = _candidate(
        genesis, signer, key, CandidateEffect("custom.emit", {}), keypart="fresh"
    )

    def emits_generated_shape(effect, candidate, receipt_id, _context):
        direct = create_runtime_record(
            "custom.fact",
            {"candidate_id": candidate.id, "effect_type": effect.effect_type},
            causal_parents=(receipt_id,),
        )
        forged_coeffect = create_runtime_record(
            "allowance.granted",
            {"candidate_id": candidate.id, "amount": 1},
            causal_parents=(receipt_id,),
        )
        return EffectProjection((direct, forged_coeffect), relation_target="fresh")

    decision = CandidateCommitter(
        store,
        store,
        additional_effects={
            "custom.emit": EffectHandler(
                "custom.emit",
                emits_generated_shape,
                direct_output_fact_types=frozenset({"custom.fact"}),
            )
        },
    ).commit(proposal, genesis)
    assert not decision.accepted
    assert "undeclared direct fact type" in next(
        record.payload["reason"]
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    assert store.scan_runtime_records(record_type="allowance.granted") == []
    store.close()


@pytest.mark.parametrize(
    "bad_projection",
    [
        lambda: EffectProjection((None,), relation_target="bad"),
        lambda: EffectProjection(None, relation_target="bad"),
        lambda: {"records": (), "relation_target": "bad"},
        lambda: EffectProjection((object(),), relation_target="bad"),
        lambda: EffectProjection(
            (replace(create_runtime_record("custom.fact", {}), id="record:forged"),),
            relation_target="bad",
        ),
    ],
)
def test_malformed_projector_records_are_durably_rejected(tmp_path, bad_projection):
    store, signer, key, genesis = _env(tmp_path)
    proposal = _candidate(
        genesis,
        signer,
        key,
        CandidateEffect("custom.emit", {}),
        keypart="malformed-projector",
    )

    def malformed(_effect, _candidate, _receipt_id, _context):
        return bad_projection()

    decision = CandidateCommitter(
        store,
        store,
        additional_effects={"custom.emit": ("custom.emit", malformed)},
    ).commit(proposal, genesis)

    assert not decision.accepted
    assert store.scan_runtime_records(record_type="candidate.received")
    rejections = store.scan_runtime_records(record_type="candidate.rejected")
    assert rejections
    assert store.scan_runtime_records(record_type="custom.fact") == []
    store.close()
