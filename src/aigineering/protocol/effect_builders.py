"""Pure builders for standard typed Candidate effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.protocol.candidate import ActorKey, CandidateEffect
from aigineering.protocol.envelope import CandidateEnvelope
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
                "created_by": asset.created_by,
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


def asset_attestation_effect(
    contract_id: str,
    output_name: str,
    asset_id: str,
    *,
    verdict: str = "accepted",
    evidence_asset_ids: tuple[str, ...] = (),
) -> CandidateEffect:
    """Attest one exact output Asset without copying or replacing its content."""
    return CandidateEffect(
        "asset.attest",
        {
            "contract_id": contract_id,
            "output_name": output_name,
            "asset_id": asset_id,
            "verdict": verdict,
            "evidence_asset_ids": list(evidence_asset_ids),
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
                "actor_id": registration.actor_id,
                "key_id": registration.key_id,
            }
        },
    )


def worker_claim_effect(
    worker_id: str,
    *,
    contract_id: str | None = None,
    lease_seconds: int = 60,
) -> CandidateEffect:
    """Authenticate one operational claim request without making it a fact effect."""
    return CandidateEffect(
        "worker.claim",
        {
            "worker_id": worker_id,
            "contract_id": contract_id,
            "lease_seconds": lease_seconds,
        },
    )


def worker_claim_renewal_effect(
    worker_id: str,
    claim_id: str,
    claim_epoch: int,
    *,
    lease_seconds: int = 60,
) -> CandidateEffect:
    """Authenticate one fenced lease-renewal request."""
    return CandidateEffect(
        "worker.claim.renew",
        {
            "worker_id": worker_id,
            "claim_id": claim_id,
            "claim_epoch": claim_epoch,
            "lease_seconds": lease_seconds,
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


def actor_revocation_effect(actor_id: str, key_id: str, reason: str) -> CandidateEffect:
    return CandidateEffect(
        "actor.revoke",
        {"actor_id": actor_id, "key_id": key_id, "reason": reason},
    )


def actor_rotation_effect(
    current_key_id: str, replacement_key: ActorKey, reason: str
) -> CandidateEffect:
    return CandidateEffect(
        "actor.rotate",
        {
            "current_key_id": current_key_id,
            "reason": reason,
            "replacement_key": {
                "actor_id": replacement_key.actor_id,
                "capabilities": list(replacement_key.capabilities),
                "key_id": replacement_key.key_id,
                "kind": replacement_key.kind,
                "public_key": replacement_key.public_key,
            },
        },
    )


def worker_output_effect(envelope: CandidateEnvelope) -> CandidateEffect:
    """Wrap one claim-bound worker result in an authenticated Candidate effect."""
    return CandidateEffect("worker.output", {"envelope": envelope.to_dict()})


def task_delegation_effect(envelope: CandidateEnvelope) -> CandidateEffect:
    """Propose claim-bound task delegation instead of a runtime fact output."""
    return CandidateEffect("task.delegate", {"envelope": envelope.to_dict()})
