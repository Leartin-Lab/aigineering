"""Consume the language-neutral signed Worker-assignment vector."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from aigineering.protocol.candidate import CandidateEffect
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_assignment import project_task_assignment
from aigineering.protocol.candidate import (
    candidate_proposal_to_dict,
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    create_candidate_proposal,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    task_assign_effect,
    task_handoff_effect,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_to_dict
from aigineering.protocol.signing import Ed25519Signer


def _record(value):
    return create_runtime_record(
        value["record_type"],
        value["payload"],
        causal_parents=value["causal_parents"],
        schema_version=value["schema_version"],
    )


def test_public_task_assignment_vectors():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-assignment-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    candidate_value = vectors["effect_vector"]["candidate"]
    candidate = candidate_proposal_from_dict(candidate_value["value"])
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    verify_candidate_proposal(candidate, genesis)
    assert (
        candidate_signing_bytes(candidate).decode()
        == candidate_value["expected_signing_utf8"]
    )
    vector_contract = Contract(**vectors["contract"])
    assert deep_thaw(contract_to_dict(vector_contract)) == vectors["contract"]
    context_records = tuple(_record(item) for item in vectors["context_records"])
    registration = WorkerRegistration(**vectors["registration"])
    effect = task_assign_effect(
        vector_contract.id,
        registration.worker_id,
        "",
        "assign exact work",
    )
    assert candidate.effects == (effect,)
    receipt = candidate_received_record(candidate, genesis)
    vector_receipt = vectors["effect_vector"]["receipt"]
    assert receipt.id == vector_receipt["id"]
    assert receipt.record_type == vector_receipt["record_type"]
    assert deep_thaw(receipt.payload) == vector_receipt["payload"]
    assert list(receipt.causal_parents) == vector_receipt["causal_parents"]
    projection = project_task_assignment(
        effect,
        candidate,
        receipt.id,
        EffectProjectionContext(
            contracts=(vector_contract,),
            runtime_records=context_records,
            registrations=(registration,),
        ),
    )
    actual = [
        {
            "id": record.id,
            "record_type": record.record_type,
            "schema_version": record.schema_version,
            "payload": deep_thaw(record.payload),
            "causal_parents": list(record.causal_parents),
        }
        for record in projection.records
    ]
    assert actual == vectors["effect_vector"]["records"]


def test_task_assignment_rejects_mixed_candidate_effects():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-assignment-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    candidate = candidate_proposal_from_dict(
        vectors["effect_vector"]["candidate"]["value"]
    )
    effect = candidate.effects[0]
    mixed = replace(
        candidate,
        effects=(
            effect,
            CandidateEffect(
                "task.comment", {"contract_id": "x", "text": "x", "reply_to": ""}
            ),
        ),
    )
    with pytest.raises(ValueError, match="exactly one effect"):
        project_task_assignment(
            effect,
            mixed,
            vectors["effect_vector"]["receipt"]["id"],
            EffectProjectionContext(),
        )


def test_successor_override_set_and_clear_signed_vectors():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-assignment-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    signer = Ed25519Signer.from_private_key_hex(
        vectors["ed25519_test_key"]["private_key_hex"]
    )
    source_id = vectors["contract"]["id"]
    for item in vectors["successor_override_vectors"]:
        override = item["assignment_override"]
        effect = task_handoff_effect(
            source_id,
            f"{item['name']} successor Worker",
            budget=2,
            assignment_override=override,
        )
        candidate = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id="human:assignment-vector",
            key_id="assignment-vector-key",
            effects=(effect,),
            signer=signer,
            idempotency_key=f"assignment-successor-{item['name']}-1",
        )
        wire = item["candidate"]
        assert candidate_proposal_to_dict(candidate) == wire["value"]
        assert (
            candidate_signing_bytes(candidate).decode("utf-8")
            == wire["expected_signing_utf8"]
        )
        verify_candidate_proposal(candidate, genesis)
