"""Consume the public task-comment vector through the Python implementation."""

import json
from pathlib import Path

from aigineering.core.ids import canonical_json
from aigineering.plugins.collaboration import project_task_comment
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import task_comment_effect
from aigineering.protocol.wire import contract_from_dict


def test_public_task_comment_vector():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-comment-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        canonical_json(vectors["canonical_json"]["input"])
        == vectors["canonical_json"]["expected"]
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    contract = contract_from_dict(vectors["contract"]["value"])
    candidate = candidate_proposal_from_dict(vectors["comment_candidate"]["value"])
    verify_candidate_proposal(candidate, genesis)
    assert (
        candidate_signing_bytes(candidate).decode()
        == vectors["comment_candidate"]["expected_signing_utf8"]
    )
    payload = candidate.effects[0].payload
    assert candidate.effects[0] == task_comment_effect(
        contract.id, payload["text"], reply_to=payload["reply_to"]
    )
    receipt = candidate_received_record(candidate, genesis)
    record = project_task_comment(
        candidate.effects[0],
        candidate,
        receipt.id,
        contracts=(contract,),
        runtime_records=(),
    )
    assert {
        "id": record.id,
        "record_type": record.record_type,
        "schema_version": record.schema_version,
        "payload": dict(record.payload),
        "causal_parents": list(record.causal_parents),
    } == vectors["comment_record"]
