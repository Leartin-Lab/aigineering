"""Consume the language-neutral signed dependency vectors."""

import json
from pathlib import Path

from aigineering.core.ids import canonical_json
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.task_dependencies import TASK_DEPENDENCY_EFFECTS
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    task_dependency_link_effect,
    task_dependency_unlink_effect,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract


def test_public_task_dependency_vectors():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-dependency-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        canonical_json(vectors["canonical_json"]["input"])
        == vectors["canonical_json"]["expected"]
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    contracts = tuple(Contract(**item) for item in vectors["contracts"])
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
        if effect.effect_type == "task.dependency.link":
            expected = task_dependency_link_effect(
                payload["contract_id"],
                payload["prerequisite_contract_id"],
                payload["reason"],
            )
        else:
            expected = task_dependency_unlink_effect(
                payload["contract_id"], payload["constraint_id"], payload["reason"]
            )
        assert effect == expected
        handler = TASK_DEPENDENCY_EFFECTS[effect.effect_type]
        projection = handler.projector(
            effect,
            candidate,
            candidate_received_record(candidate, genesis).id,
            EffectProjectionContext(contracts=contracts, runtime_records=history),
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
        history += projection.records
