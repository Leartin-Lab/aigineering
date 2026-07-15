"""Pure builders for standard typed Candidate effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.protocol.candidate import ActorKey, CandidateEffect
from aigineering.protocol.types import Asset, Contract, ReplacementClaim
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
                "lineage_id": asset.lineage_id,
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


def replacement_claim_effect(claim: ReplacementClaim) -> CandidateEffect:
    return CandidateEffect(
        "asset.relate",
        {
            "claim": {
                "claim_type": claim.claim_type,
                "definition_hash": claim.definition_hash,
                "lineage_id": claim.lineage_id,
                "replacement_asset_id": claim.replacement_asset_id,
                "source_asset_id": claim.source_asset_id,
            }
        },
    )


def contract_cancellation_effect(contract_id: str, reason: str) -> CandidateEffect:
    return CandidateEffect(
        "contract.cancel",
        {"contract_id": contract_id, "reason": reason},
    )


def actor_authorization_effect(actor_key: ActorKey) -> CandidateEffect:
    return CandidateEffect(
        "actor.authorize",
        {
            "actor_key": {
                "actor_id": actor_key.actor_id,
                "capabilities": list(actor_key.capabilities),
                "key_id": actor_key.key_id,
                "kind": actor_key.kind,
                "public_key": actor_key.public_key,
            }
        },
    )
