"""Consume the language-neutral signed task-promotion vector in Python."""

from __future__ import annotations

import json
from pathlib import Path

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.collaboration import project_task_promote_effect
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import task_promote_effect
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    runtime_record_effective_payload,
)
from aigineering.protocol.wire import contract_from_dict


def _record(value: dict) -> RuntimeRecord:
    return RuntimeRecord(
        id=value["id"],
        record_type=value["record_type"],
        payload=value["payload"],
        causal_parents=tuple(value["causal_parents"]),
        schema_version=value["schema_version"],
        recorded_at="2026-10-05T00:00:00+00:00",
    )


def test_public_task_promotion_vector():
    vector = json.loads(
        Path("conformance/v0.5.13/task-promotion-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    genesis = genesis_manifest_from_dict(vector["genesis"])
    contract = contract_from_dict(vector["contract"]["value"])
    candidate = candidate_proposal_from_dict(vector["candidate"]["value"])
    verify_candidate_proposal(candidate, genesis)
    assert (
        candidate_signing_bytes(candidate).decode()
        == vector["candidate"]["expected_signing_utf8"]
    )
    assert candidate.effects == (
        task_promote_effect(
            contract.id,
            tuple(candidate.effects[0].payload["hold_ids"]),
            candidate.effects[0].payload["reason"],
        ),
    )
    records = tuple(_record(item) for item in vector["context_records"])
    receipt = candidate_received_record(candidate, genesis)
    assert receipt.id == vector["receipt"]["id"]
    projected = project_task_promote_effect(
        candidate.effects[0],
        candidate,
        receipt.id,
        EffectProjectionContext(contracts=(contract,), runtime_records=records),
    )
    assert [record.id for record in projected.records] == [
        item["id"] for item in vector["records"]
    ]
    assert [
        runtime_record_effective_payload(record) for record in projected.records
    ] == [
        {
            "causal_parents": item["causal_parents"],
            "payload": item["payload"],
            "record_type": item["record_type"],
            "schema_version": item["schema_version"],
        }
        for item in vector["records"]
    ]
