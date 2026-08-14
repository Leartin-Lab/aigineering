"""Pure builders for standard typed Candidate effects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from aigineering.protocol.candidate import ActorKey, CandidateEffect
from aigineering.protocol.asset_graph import (
    ContentObject,
    DefinitionContentAssertion,
    SignedAssetDefinition,
    content_object_to_dict,
    create_content_object,
    create_definition_content_assertion,
    create_signed_definition,
    definition_content_assertion_to_dict,
    signed_definition_to_dict,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Asset, Contract, ReplacementClaim
from aigineering.protocol.wire import contract_to_dict

if TYPE_CHECKING:
    from aigineering.core.signing import Signer
    from aigineering.core.worker_routing import WorkerRegistration


def contract_declaration_effect(
    contract: Contract, *, atomic_group: str = ""
) -> CandidateEffect:
    return CandidateEffect(
        "contract.declare",
        {"contract": contract_to_dict(contract)},
        atomic_group=atomic_group,
    )


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


def content_publication_effect(content: ContentObject) -> CandidateEffect:
    return CandidateEffect(
        "asset.content.publish", {"content": content_object_to_dict(content)}
    )


def definition_publication_effect(
    definition: SignedAssetDefinition,
) -> CandidateEffect:
    return CandidateEffect(
        "asset.definition.publish",
        {"definition": signed_definition_to_dict(definition)},
    )


def definition_content_assertion_effect(
    assertion: DefinitionContentAssertion,
) -> CandidateEffect:
    return CandidateEffect(
        "asset.assert",
        {"assertion": definition_content_assertion_to_dict(assertion)},
    )


def asset_attestation_effect(
    contract_id: str,
    output_name: str,
    asset_id: str,
    *,
    policy_id: str,
    policy_version: str,
    verdict: str = "accepted",
    rubric_asset_ids: tuple[str, ...] = (),
    evidence_asset_ids: tuple[str, ...] = (),
    atomic_group: str = "",
) -> CandidateEffect:
    """Attest one exact output Asset without copying or replacing its content."""
    return CandidateEffect(
        "asset.attest",
        {
            "contract_id": contract_id,
            "output_name": output_name,
            "asset_id": asset_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "verdict": verdict,
            "rubric_asset_ids": list(rubric_asset_ids),
            "evidence_asset_ids": list(evidence_asset_ids),
        },
        atomic_group=atomic_group,
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
                "derivation_version": claim.derivation_version,
                "range_spec": claim.range_spec,
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


def claim_bound_output_effects(
    envelope: CandidateEnvelope,
) -> tuple[CandidateEffect, ...]:
    """Translate one parsed `/exec` assertion into ordinary Asset effects."""
    return tuple(
        CandidateEffect(
            "asset.propose",
            {
                "asset": {
                    "content": content,
                    "content_type": "text",
                    "created_by": envelope.contract_id,
                    "name": name,
                    "origin": "worker",
                    "promptable": True,
                    "trust_tier": "observed",
                }
            },
            atomic_group=f"output:{envelope.contract_id}",
        )
        for name, content in _claim_bound_outputs(envelope)
    )


def _claim_bound_outputs(envelope: CandidateEnvelope) -> tuple[tuple[str, str], ...]:
    parsed = envelope.parsed_action
    if not isinstance(parsed, Mapping) or parsed.get("type") != "exec":
        raise ValueError("claim-bound output effects require a parsed /exec action")
    outputs = parsed.get("outputs")
    if not isinstance(outputs, Mapping) or not outputs:
        raise ValueError("claim-bound /exec requires non-empty outputs")
    if not all(
        isinstance(name, str) and isinstance(content, str)
        for name, content in outputs.items()
    ):
        raise ValueError("claim-bound /exec outputs must map strings to strings")
    return tuple(sorted(outputs.items()))


def claim_bound_graph_output_effects(
    envelope: CandidateEnvelope,
    contract: Contract,
    *,
    domain_id: str,
    actor_id: str,
    key_id: str,
    signer: Signer,
) -> tuple[CandidateEffect, ...]:
    """Build one atomic signed graph assertion batch for claimed `/exec` output."""
    if contract.id != envelope.contract_id:
        raise ValueError("claim-bound graph output requires its claimed Contract")
    group = f"output:{contract.id}"
    effects: list[CandidateEffect] = []
    published_content_ids: set[str] = set()
    for name, raw_content in _claim_bound_outputs(envelope):
        content = create_content_object(raw_content)
        definition = create_signed_definition(
            domain_id=domain_id,
            name=name,
            description=f"Declared output {name!r} of Contract {contract.id}",
            content_type="text",
            source_kind="contract-output",
            source_uri=contract.id,
            actor_id=actor_id,
            key_id=key_id,
            signer=signer,
        )
        assertion = create_definition_content_assertion(
            domain_id=domain_id,
            definition_id=definition.id,
            content_id=content.id,
            relation_type="satisfies",
            actor_id=actor_id,
            key_id=key_id,
            signer=signer,
            evidence={"contract_id": contract.id, "output_name": name},
        )
        if content.id not in published_content_ids:
            effect = content_publication_effect(content)
            effects.append(CandidateEffect(effect.effect_type, effect.payload, group))
            published_content_ids.add(content.id)
        for effect in (
            definition_publication_effect(definition),
            definition_content_assertion_effect(assertion),
        ):
            effects.append(CandidateEffect(effect.effect_type, effect.payload, group))
    return tuple(effects)
