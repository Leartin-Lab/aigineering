"""Consume the public signed priority vector through the pure projector."""

import json
from pathlib import Path

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.task_priority import task_priority_projector
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import task_priority_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import contract_from_dict


def test_public_task_priority_vector():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-priority-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    contract = contract_from_dict(vectors["contract"])
    context_records = tuple(
        create_runtime_record(
            item["record_type"],
            item["payload"],
            causal_parents=item["causal_parents"],
            schema_version=item["schema_version"],
        )
        for item in vectors["context_records"]
    )
    runtime_records = context_records
    for item in vectors["effect_vectors"]:
        wire = item["candidate"]
        candidate = candidate_proposal_from_dict(wire["value"])
        verify_candidate_proposal(candidate, genesis)
        assert (
            candidate_signing_bytes(candidate).decode() == wire["expected_signing_utf8"]
        )
        effect = candidate.effects[0]
        assert effect == task_priority_effect(
            contract.id,
            effect.payload["priority"],
            effect.payload["previous_priority_id"],
            effect.payload["reason"],
        )
        receipt = candidate_received_record(candidate, genesis)
        assert receipt.id == item["receipt"]["id"]
        projection = task_priority_projector(
            effect,
            candidate,
            receipt.id,
            EffectProjectionContext(
                contracts=(contract,), runtime_records=runtime_records
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
        assert actual == item["records"]
        committed = create_runtime_record(
            "candidate.committed",
            {
                "candidate_id": candidate.id,
                "committed_record_ids": [record.id for record in projection.records],
            },
            causal_parents=(receipt.id, *(record.id for record in projection.records)),
        )
        runtime_records += (receipt, *projection.records, committed)
