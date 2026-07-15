"""Built-in typed-effect projectors for the commitment reducer."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from aigineering.core.authority import matched_reserved_prefix
from aigineering.core.actor_facts import actor_key_payload
from aigineering.core.asset_versions import replacement_claim_payload
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.fact_materialization import asset_committed_record
from aigineering.core.ids import hash_asset_content, hash_asset_definition, hash_claim
from aigineering.core.provenance import sign_asset
from aigineering.core.worker_routing import (
    WorkerRegistration,
    worker_registration_payload,
)
from aigineering.protocol.candidate import ActorKey, CandidateEffect, CandidateProposal
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Asset, Contract, ReplacementClaim
from aigineering.protocol.wire import contract_to_dict


@dataclass(frozen=True)
class EffectProjection:
    records: tuple[RuntimeRecord, ...]
    relation_target: str
    contract: Contract | None = None
    assets: tuple[Asset, ...] = ()
    accepted_asset_names: tuple[str, ...] = ()
    additional_capabilities: tuple[str, ...] = ()


def _contract_from_payload(payload: Mapping[str, Any]) -> Contract:
    value = payload.get("contract")
    if not isinstance(value, Mapping):
        raise ValueError("contract.declare requires an object payload.contract")
    data = deep_thaw(value)
    return Contract(
        id=str(data.get("id", "")),
        parent_id=data.get("parent_id"),
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        inputs=tuple(data.get("inputs", ())),
        outputs=tuple(data.get("outputs", ())),
        activation=str(data.get("activation", "")),
        budget=int(data.get("budget", 0)),
        tool_scope=tuple(data.get("tool_scope", ())),
        labels=tuple(data.get("labels", ())),
        worker_capabilities=tuple(data.get("worker_capabilities", ())),
        worker_pools=tuple(data.get("worker_pools", ())),
        origin=str(data.get("origin", "human")),
        minting_authority=tuple(data.get("minting_authority", ())),
        sensitive_input_policy=data.get("sensitive_input_policy"),
    )


def project_contract_declaration(
    effect: CandidateEffect, candidate: CandidateProposal, receipt_id: str
) -> EffectProjection:
    contract = _contract_from_payload(effect.payload)
    validate_contract_commitment(contract)
    record = create_runtime_record(
        "contract.declared",
        {"candidate_id": candidate.id, "contract": contract_to_dict(contract)},
        causal_parents=(receipt_id,),
    )
    return EffectProjection(
        records=(record,),
        relation_target=contract.id,
        contract=contract,
        additional_capabilities=(
            ("contract.publish.protected",) if contract.minting_authority else ()
        ),
    )


def project_asset_proposal(
    effect: CandidateEffect, candidate: CandidateProposal, receipt_id: str
) -> EffectProjection:
    value = effect.payload.get("asset")
    if not isinstance(value, Mapping):
        raise ValueError("asset.propose requires an object payload.asset")
    data = deep_thaw(value)
    name = str(data.get("name", ""))
    content = str(data.get("content", ""))
    if not name:
        raise ValueError("asset.propose asset.name must not be empty")
    prefix = matched_reserved_prefix(name)
    content_hash = hash_asset_content(name, content)
    asset = sign_asset(
        Asset(
            id=content_hash,
            name=name,
            content=content,
            content_type=str(data.get("content_type", "text")),
            created_by=candidate.actor_id,
            origin=str(data.get("origin", "human")),
            trust_tier=str(data.get("trust_tier", "human")),
            source_uri=str(data.get("source_uri", "")),
            signed_by=candidate.actor_id,
            signer_kind=f"candidate:{candidate.signature_kind}",
            promptable=bool(data.get("promptable", True)),
            disclosure_view=str(data.get("disclosure_view", "original")),
            lineage_id=str(data.get("lineage_id", "")),
            definition_hash=hash_asset_definition(name),
            content_hash=content_hash,
        ),
        signed_by=candidate.actor_id,
    )
    record = asset_committed_record(asset, causal_parents=(receipt_id,))
    return EffectProjection(
        records=(record,),
        relation_target=asset.id,
        assets=(asset,),
        accepted_asset_names=(asset.name,),
        additional_capabilities=(
            ("asset.publish.protected",) if prefix is not None else ()
        ),
    )


def project_worker_registration(
    effect: CandidateEffect, candidate: CandidateProposal, receipt_id: str
) -> EffectProjection:
    del candidate
    value = effect.payload.get("registration")
    if not isinstance(value, Mapping):
        raise ValueError("worker.register requires an object payload.registration")
    data = deep_thaw(value)
    registration = WorkerRegistration(
        worker_id=str(data.get("worker_id", "")),
        capabilities=tuple(data.get("capabilities", ())),
        pools=tuple(data.get("pools", ())),
        profile_id=str(data.get("profile_id", "")),
        capacity=int(data.get("capacity", 1)),
        enabled=bool(data.get("enabled", True)),
        version=str(data.get("version", "1")),
    )
    record = create_runtime_record(
        "worker.registered",
        worker_registration_payload(registration),
        causal_parents=(receipt_id,),
    )
    return EffectProjection(
        records=(record,),
        relation_target=registration.worker_id,
    )


def project_replacement_claim(
    effect: CandidateEffect, candidate: CandidateProposal, receipt_id: str
) -> EffectProjection:
    value = effect.payload.get("claim")
    if not isinstance(value, Mapping):
        raise ValueError("asset.relate requires an object payload.claim")
    data = deep_thaw(value)
    source_asset_id = str(data.get("source_asset_id", ""))
    replacement_asset_id = str(data.get("replacement_asset_id", ""))
    claim_type = str(data.get("claim_type", "replacement"))
    if not source_asset_id or not replacement_asset_id:
        raise ValueError("asset.relate requires both asset identifiers")
    claim = ReplacementClaim(
        id=hash_claim(source_asset_id, replacement_asset_id, claim_type),
        source_asset_id=source_asset_id,
        replacement_asset_id=replacement_asset_id,
        definition_hash=str(data.get("definition_hash", "")),
        claim_type=claim_type,
        signed_by=candidate.actor_id,
        provenance_seal=candidate.signature,
        lineage_id=str(data.get("lineage_id", "")),
    )
    record = create_runtime_record(
        "replacement.claimed",
        {"claim": replacement_claim_payload(claim)},
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=claim.id)


def project_contract_cancellation(
    effect: CandidateEffect, candidate: CandidateProposal, receipt_id: str
) -> EffectProjection:
    contract_id = str(effect.payload.get("contract_id", ""))
    reason = str(effect.payload.get("reason", ""))
    if not contract_id or not reason:
        raise ValueError("contract.cancel requires contract_id and reason")
    record = create_runtime_record(
        "lifecycle.terminal",
        {
            "actor_id": candidate.actor_id,
            "contract_id": contract_id,
            "reason": reason,
            "terminal": "cancelled",
        },
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=contract_id)


def project_actor_authorization(
    effect: CandidateEffect, candidate: CandidateProposal, receipt_id: str
) -> EffectProjection:
    value = effect.payload.get("actor_key")
    if not isinstance(value, Mapping):
        raise ValueError("actor.authorize requires an object payload.actor_key")
    data = deep_thaw(value)
    key = ActorKey(
        actor_id=str(data.get("actor_id", "")),
        key_id=str(data.get("key_id", "")),
        kind=str(data.get("kind", "")),
        public_key=str(data.get("public_key", "")),
        capabilities=tuple(data.get("capabilities", ())),
    )
    if key.kind in {"deterministic", "asig_"}:
        raise ValueError("actor authorization requires an authenticating key kind")
    record = create_runtime_record(
        "actor.authorized",
        {**actor_key_payload(key), "authorized_by": candidate.actor_id},
        causal_parents=(receipt_id,),
    )
    return EffectProjection(
        records=(record,), relation_target=f"{key.actor_id}/{key.key_id}"
    )


def project_actor_revocation(
    effect: CandidateEffect, candidate: CandidateProposal, receipt_id: str
) -> EffectProjection:
    actor_id = str(effect.payload.get("actor_id", ""))
    key_id = str(effect.payload.get("key_id", ""))
    reason = str(effect.payload.get("reason", ""))
    if not actor_id or not key_id or not reason:
        raise ValueError("actor.revoke requires actor_id, key_id, and reason")
    record = create_runtime_record(
        "actor.revoked",
        {
            "actor_id": actor_id,
            "key_id": key_id,
            "reason": reason,
            "revoked_by": candidate.actor_id,
        },
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=f"{actor_id}/{key_id}")


EffectProjector = Callable[[CandidateEffect, CandidateProposal, str], EffectProjection]
BUILTIN_EFFECTS: Mapping[str, tuple[str, EffectProjector]] = MappingProxyType(
    {
        "asset.propose": ("asset.publish", project_asset_proposal),
        "contract.declare": ("contract.publish", project_contract_declaration),
        "worker.register": ("worker.register", project_worker_registration),
        "asset.relate": ("asset.relate", project_replacement_claim),
        "contract.cancel": ("contract.cancel", project_contract_cancellation),
        "actor.authorize": ("actor.authorize", project_actor_authorization),
        "actor.revoke": ("actor.revoke", project_actor_revocation),
    }
)
