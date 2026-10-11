"""Consume language-neutral revision vectors against exact pure input facts."""

import json
from pathlib import Path

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.task_revision import TASK_REVISION_EFFECTS
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    task_revision_effect,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    runtime_record_effective_payload,
)
from aigineering.protocol.wire import (
    asset_from_dict,
    contract_from_dict,
    contract_to_dict,
)


def test_public_task_revision_vectors():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-revision-vectors.json").read_text()
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    for item in vectors["effect_vectors"]:
        candidate = candidate_proposal_from_dict(item["candidate"]["value"])
        verify_candidate_proposal(candidate, genesis)
        assert (
            candidate_signing_bytes(candidate).decode()
            == item["candidate"]["expected_signing_utf8"]
        )
        effect = candidate.effects[0]
        payload = effect.payload
        assert effect == task_revision_effect(
            payload["decision_id"], payload["reason"], budget=payload["budget"]
        )
        wire_context = item["context"]
        context = EffectProjectionContext(
            contracts=tuple(contract_from_dict(c) for c in wire_context["contracts"]),
            assets=tuple(asset_from_dict(a) for a in wire_context["assets"]),
            runtime_records=tuple(
                RuntimeRecord(**r) for r in wire_context["runtime_records"]
            ),
        )
        projection = TASK_REVISION_EFFECTS[effect.effect_type].projector(
            effect,
            candidate,
            candidate_received_record(candidate, genesis).id,
            context,
        )
        records = [
            {"id": r.id, **runtime_record_effective_payload(r)}
            for r in projection.records
        ]
        assert deep_thaw(records) == item["records"]
        assert (
            list(projection.additional_capabilities) == item["additional_capabilities"]
        )
        assert (
            deep_thaw(contract_to_dict(projection.contract))
            if projection.contract
            else None
        ) == item["contract"]
