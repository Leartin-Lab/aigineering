"""Consume the language-neutral paired Worker profile promotion vector."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from aigineering.core.effect_projectors import project_worker_registration
from aigineering.core.ids import compute_content_hash
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.worker_routing import (
    WorkerRegistration,
    worker_registration_payload,
)
from aigineering.plugins.worker_profile_promotion import (
    WORKER_PROFILE_PROMOTION_EFFECTS,
    completed_profile_promotion_matches,
    project_worker_profile_promotion,
    validate_worker_profile_promotion,
)
from aigineering.protocol.candidate import (
    ActorKey,
    candidate_proposal_from_dict,
    candidate_proposal_to_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import worker_profile_promotion_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.signing import Ed25519Signer


def _record(value):
    return create_runtime_record(
        value["record_type"],
        value["payload"],
        causal_parents=value["causal_parents"],
        schema_version=value["schema_version"],
    )


def test_worker_profile_promotion_vector_projects_exact_paired_facts():
    vectors = json.loads(
        Path("conformance/v0.5.13/worker-profile-promotion-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    vector = vectors["candidate_vector"]
    candidate = candidate_proposal_from_dict(vector["candidate"]["value"])
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    verify_candidate_proposal(candidate, genesis)
    assert candidate_proposal_to_dict(candidate) == vector["candidate"]["value"]
    assert (
        candidate_signing_bytes(candidate).decode()
        == vector["candidate"]["expected_signing_utf8"]
    )

    worker_signer = Ed25519Signer.from_private_key_hex("22" * 32)
    worker_actor_key = ActorKey(
        vectors["worker_key"]["actor_id"],
        vectors["worker_key"]["key_id"],
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    prior_payload = vectors["prior_registration"]
    prior_registration = WorkerRegistration(
        **{
            key: prior_payload[key]
            for key in worker_registration_payload(WorkerRegistration("x"))
        }
    )
    prior_record = create_runtime_record(
        "worker.registered",
        prior_payload,
        causal_parents=("record:bootstrap-receipt",),
    )
    assert candidate.effects[0].payload["previous_registration_id"] == prior_record.id
    context = EffectProjectionContext(
        runtime_records=(prior_record,),
        actor_keys=(worker_actor_key,),
        registrations=(prior_registration,),
    )
    receipt = candidate_received_record(candidate, genesis)
    actual_receipt = {
        "id": receipt.id,
        "record_type": receipt.record_type,
        "schema_version": receipt.schema_version,
        "payload": deep_thaw(receipt.payload),
        "causal_parents": list(receipt.causal_parents),
    }
    assert actual_receipt == vector["receipt"]

    promotion_effect, registration_effect = candidate.effects
    projection = project_worker_profile_promotion(
        promotion_effect, candidate, receipt.id, context
    )
    registration_projection = project_worker_registration(
        registration_effect, candidate, receipt.id, context
    )
    records = projection.records + registration_projection.records
    actual_records = [
        {
            "id": record.id,
            "record_type": record.record_type,
            "schema_version": record.schema_version,
            "payload": deep_thaw(record.payload),
            "causal_parents": list(record.causal_parents),
        }
        for record in records
    ]
    assert actual_records == vector["records"]
    audit, registered = records
    assert set(audit.payload) == {
        "worker_id",
        "previous_registration_id",
        "registration_id",
        "previous_version",
        "version",
        "profile_id",
        "actor_id",
        "key_id",
        "candidate_id",
    }
    assert audit.causal_parents == (receipt.id, prior_record.id, registered.id)
    assert registered.causal_parents == (receipt.id,)
    assert audit.payload["actor_id"] == prior_registration.actor_id
    assert audit.payload["key_id"] == prior_registration.key_id
    assert candidate.actor_id != audit.payload["actor_id"]
    assert candidate.key_id != audit.payload["key_id"]
    assert worker_registration_payload(prior_registration)["version"] == "1"
    assert WORKER_PROFILE_PROMOTION_EFFECTS[
        "worker.profile.promote"
    ].direct_output_fact_types == frozenset({"worker.profile.promoted"})
    committed = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [audit.id, registered.id],
        },
        causal_parents=(receipt.id, audit.id, registered.id),
    )
    replay_context = EffectProjectionContext(
        runtime_records=(
            prior_record,
            receipt,
            audit,
            registered,
            committed,
        ),
        actor_keys=(worker_actor_key,),
        registrations=(
            WorkerRegistration(
                worker_id=audit.payload["worker_id"],
                capabilities=tuple(registered.payload["capabilities"]),
                pools=tuple(registered.payload["pools"]),
                profile_id=registered.payload["profile_id"],
                capacity=registered.payload["capacity"],
                enabled=registered.payload["enabled"],
                version=registered.payload["version"],
                actor_id=registered.payload["actor_id"],
                key_id=registered.payload["key_id"],
            ),
        ),
    )
    replay = project_worker_profile_promotion(
        promotion_effect, candidate, receipt.id, replay_context
    )
    assert replay.records == (audit,)
    assert completed_profile_promotion_matches(
        replay_context.runtime_records,
        requested=replay_context.registrations[0],
        current=replay_context.registrations[0],
    )

    reordered_head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [registered.id, audit.id],
        },
        causal_parents=(receipt.id, registered.id, audit.id),
    )
    reordered_context = replace(
        replay_context,
        runtime_records=(prior_record, receipt, audit, registered, reordered_head),
    )
    with pytest.raises(ValueError, match="do not match Candidate"):
        project_worker_profile_promotion(
            promotion_effect, candidate, receipt.id, reordered_context
        )


def test_worker_profile_promotion_rejects_unpaired_or_mixed_candidate():
    vectors = json.loads(
        Path("conformance/v0.5.13/worker-profile-promotion-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    vector = vectors["candidate_vector"]
    candidate = candidate_proposal_from_dict(vector["candidate"]["value"])
    effect = candidate.effects[0]
    mixed = replace(
        candidate,
        effects=(effect, *candidate.effects[1:], candidate.effects[1]),
    )
    with pytest.raises(ValueError, match="two-effect Candidate"):
        project_worker_profile_promotion(
            effect, mixed, vector["receipt"]["id"], EffectProjectionContext()
        )


def test_worker_profile_builder_uses_expected_effect_type_and_fields():
    vectors = json.loads(
        Path("conformance/v0.5.13/worker-profile-promotion-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    payload = vectors["prior_registration"]
    current = WorkerRegistration(
        **{
            key: payload[key]
            for key in worker_registration_payload(WorkerRegistration("x"))
        }
    )
    promoted = WorkerRegistration(
        **{
            **worker_registration_payload(current),
            "profile_id": "openai-compatible-v1",
            "version": "2",
        }
    )
    effect = worker_profile_promotion_effect(
        vectors["candidate_vector"]["records"][0]["causal_parents"][1], promoted
    )
    assert effect.effect_type == "worker.profile.promote"
    assert set(effect.payload) == {"previous_registration_id", "registration"}


def test_profile_promotion_rejects_zero_registration_version():
    signer = Ed25519Signer.from_private_key_hex("22" * 32)
    worker_id = "worker:zero-version"
    key_id = f"worker-{compute_content_hash(worker_id)[:16]}"
    actor_key = ActorKey(
        worker_id,
        key_id,
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    prior = WorkerRegistration(
        worker_id, version="0", actor_id=worker_id, key_id=key_id
    )
    prior_record = create_runtime_record(
        "worker.registered",
        {**worker_registration_payload(prior), "registered_by": "human:owner"},
        causal_parents=("record:bootstrap",),
    )
    context = EffectProjectionContext(
        runtime_records=(prior_record,),
        actor_keys=(actor_key,),
        registrations=(prior,),
    )
    requested = WorkerRegistration(
        worker_id,
        profile_id="openai-compatible-v1",
        version="1",
        actor_id=worker_id,
        key_id=key_id,
    )
    with pytest.raises(ValueError, match="positive registration version"):
        validate_worker_profile_promotion(
            context,
            previous_registration_id=prior_record.id,
            registration=requested,
        )
