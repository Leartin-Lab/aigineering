"""Pure builders for standard typed Candidate effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.protocol.candidate import CandidateEffect
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import contract_to_dict

if TYPE_CHECKING:
    from aigineering.core.worker_routing import WorkerRegistration


def contract_declaration_effect(contract: Contract) -> CandidateEffect:
    return CandidateEffect("contract.declare", {"contract": contract_to_dict(contract)})


def asset_proposal_effect(asset: Asset) -> CandidateEffect:
    return CandidateEffect(
        "asset.propose",
        {
            "asset": {
                "content": asset.content,
                "content_type": asset.content_type,
                "disclosure_view": asset.disclosure_view,
                "name": asset.name,
                "origin": asset.origin,
                "promptable": asset.promptable,
                "source_uri": asset.source_uri,
                "trust_tier": asset.trust_tier,
            }
        },
    )


def worker_registration_effect(registration: WorkerRegistration) -> CandidateEffect:
    return CandidateEffect(
        "worker.register",
        {
            "registration": {
                "capabilities": list(registration.capabilities),
                "capacity": registration.capacity,
                "enabled": registration.enabled,
                "pools": list(registration.pools),
                "profile_id": registration.profile_id,
                "version": registration.version,
                "worker_id": registration.worker_id,
            }
        },
    )
