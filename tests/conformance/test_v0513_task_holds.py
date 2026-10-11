"""Consume public signed hold vectors through pure Python projections."""

import json
from pathlib import Path

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import task_block_effect, task_unblock_effect
from aigineering.protocol.wire import contract_from_dict


def test_public_task_hold_vectors():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-hold-vectors.json").read_text(encoding="utf-8")
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    contract = contract_from_dict(vectors["contract"]["value"])
    history = ()
    for item in vectors["effect_vectors"]:
        candidate = candidate_proposal_from_dict(item["candidate"]["value"])
        verify_candidate_proposal(candidate, genesis)
        assert (
            candidate_signing_bytes(candidate).decode()
            == item["candidate"]["expected_signing_utf8"]
        )
        effect = candidate.effects[0]
        payload = effect.payload
        expected_effect = (
            task_block_effect(contract.id, payload["reason"])
            if effect.effect_type == "task.block"
            else task_unblock_effect(contract.id, payload["hold_id"], payload["reason"])
        )
        assert effect == expected_effect
        receipt = candidate_received_record(candidate, genesis)
        handler = COLLABORATION_EFFECTS[effect.effect_type]
        projection = handler.projector(
            effect,
            candidate,
            receipt.id,
            EffectProjectionContext(contracts=(contract,), runtime_records=history),
        )
        assert [
            {
                "id": record.id,
                "record_type": record.record_type,
                "schema_version": record.schema_version,
                "payload": dict(record.payload),
                "causal_parents": list(record.causal_parents),
            }
            for record in projection.records
        ] == item["records"]
        history += projection.records
