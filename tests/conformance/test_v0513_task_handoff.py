"""Consume language-neutral signed handoff vectors with the pure projector."""

import json
from pathlib import Path

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.handoff import HANDOFF_EFFECTS
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import task_handoff_effect
from aigineering.protocol.immutability import deep_freeze, deep_thaw
from aigineering.protocol.wire import contract_from_dict, contract_to_dict


def test_public_task_handoff_vectors():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-handoff-vectors.json").read_text()
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    source = contract_from_dict(vectors["contract"]["value"])
    for item in vectors["effect_vectors"]:
        candidate = candidate_proposal_from_dict(item["candidate"]["value"])
        verify_candidate_proposal(candidate, genesis)
        assert (
            candidate_signing_bytes(candidate).decode()
            == item["candidate"]["expected_signing_utf8"]
        )
        effect = candidate.effects[0]
        payload = effect.payload
        assert effect == task_handoff_effect(
            source.id,
            payload["reason"],
            budget=payload["budget"],
            hold_reason=payload["hold_reason"],
        )
        handler = HANDOFF_EFFECTS[effect.effect_type]
        projection = handler.projector(
            effect,
            candidate,
            candidate_received_record(candidate, genesis).id,
            EffectProjectionContext(contracts=(source,)),
        )
        assert (
            json.loads(json.dumps(contract_to_dict(projection.contract)))
            == (item["successor"])
        )
        actual = [
            {
                "id": record.id,
                "record_type": record.record_type,
                "schema_version": record.schema_version,
                "payload": dict(record.payload),
                "causal_parents": list(record.causal_parents),
            }
            for record in projection.records
        ]
        assert deep_thaw(deep_freeze(actual)) == item["records"]
