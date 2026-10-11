"""Consume signed expanded-handoff and exact-replay interoperability vectors."""

import json
from pathlib import Path

from aigineering.core.commitment import reduce_candidate
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.handoff import HANDOFF_EFFECTS
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    create_verifier,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.protocol.wire import (
    asset_from_dict,
    contract_from_dict,
    contract_to_dict,
)


def test_public_expanded_handoff_vectors():
    vectors = json.loads(
        Path("conformance/v0.5.13/expanded-handoff-vectors.json").read_text()
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    for item in vectors["effect_vectors"]:
        candidate = candidate_proposal_from_dict(item["candidate"]["value"])
        verify_candidate_proposal(candidate, genesis)
        assert (
            candidate_signing_bytes(candidate).decode()
            == item["candidate"]["expected_signing_utf8"]
        )
        context = EffectProjectionContext(
            contracts=tuple(
                contract_from_dict(value) for value in item["context"]["contracts"]
            ),
            assets=tuple(asset_from_dict(value) for value in item["context"]["assets"]),
            runtime_records=tuple(
                RuntimeRecord(**value) for value in item["context"]["runtime_records"]
            ),
        )
        for record in context.runtime_records:
            validate_runtime_record(record)
        projection = HANDOFF_EFFECTS["task.handoff"].projector(
            candidate.effects[0],
            candidate,
            candidate_received_record(candidate, genesis).id,
            context,
        )
        assert deep_thaw(contract_to_dict(projection.contract)) == item["contract"]
        assert [
            {"id": record.id, **runtime_record_effective_payload(record)}
            for record in projection.records
        ] == item["records"]
        assert (
            list(projection.additional_capabilities) == item["additional_capabilities"]
        )
        decision = reduce_candidate(
            candidate,
            genesis,
            verifier_factory=create_verifier,
            projection_context=context,
            additional_effects=HANDOFF_EFFECTS,
        )
        assert decision.accepted
