"""Pure projectors for individual built-in Candidate effect types."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from aigineering.core.acceptance import project_asset_attestation_records
from aigineering.core.actor_facts import actor_key_payload
from aigineering.core.asset_versions import replacement_claim_payload
from aigineering.core.asset_graph_facts import legacy_asset_graph_record
from aigineering.core.authority import matched_reserved_prefix
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.fact_materialization import asset_committed_record
from aigineering.core.fact_reducer import METHOD_RESULT_PREFIXES
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.ids import (
    canonical_json,
    compute_content_hash,
    hash_asset_content,
    hash_asset_definition,
    hash_claim,
)
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.provenance import sign_asset
from aigineering.core.worker_routing import (
    WorkerRegistration,
    worker_registration_payload,
)
from aigineering.protocol.candidate import ActorKey, CandidateEffect, CandidateProposal
from aigineering.protocol.asset_graph import (
    content_object_from_dict,
    content_object_to_dict,
    definition_content_assertion_from_dict,
    definition_content_assertion_to_dict,
    signed_definition_from_dict,
    signed_definition_to_dict,
    validate_content_object,
    verify_definition_content_assertion,
    verify_signed_definition,
)
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
        context_asset_ids=tuple(data.get("context_asset_ids", ())),
        worker_capabilities=tuple(data.get("worker_capabilities", ())),
        worker_pools=tuple(data.get("worker_pools", ())),
        delegation_capabilities=tuple(data.get("delegation_capabilities", ())),
        delegation_pools=tuple(data.get("delegation_pools", ())),
        origin=str(data.get("origin", "human")),
        minting_authority=tuple(data.get("minting_authority", ())),
        sensitive_input_policy=data.get("sensitive_input_policy"),
        acceptance_policy=data.get("acceptance_policy"),
    )


def project_contract_declaration(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    contract = _contract_from_payload(effect.payload)
    validate_contract_commitment(contract)
    if contract.id.startswith(("task:v4:", "task:v5:")):
        assets = {asset.id: asset for asset in context.assets}
        missing = set(contract.context_asset_ids) - assets.keys()
        if missing:
            raise ValueError("versioned Contract references unknown context Assets")
        bound_names = {assets[asset_id].name for asset_id in contract.context_asset_ids}
        if not bound_names <= set(contract.labels):
            raise ValueError(
                "versioned Contract context Asset bindings do not match labels"
            )
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
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
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
    proposed_creator = str(data.get("created_by", ""))
    is_method_result = name.startswith(METHOD_RESULT_PREFIXES)
    if is_method_result and not proposed_creator:
        raise ValueError("method-result asset.propose requires asset.created_by")
    if candidate.claim_binding is not None:
        contract = next(
            (
                item
                for item in context.contracts
                if item.id == candidate.claim_binding.contract_id
            ),
            None,
        )
        if contract is None or name not in contract.outputs:
            raise ValueError("claim-bound Asset must use a declared Contract output")
        if proposed_creator and proposed_creator != contract.id:
            raise ValueError("claim-bound Asset created_by must equal its Contract")
        created_by = contract.id
    else:
        created_by = (
            proposed_creator
            if prefix is not None and proposed_creator
            else candidate.actor_id
        )
    content_hash = hash_asset_content(name, content)
    asset_id = "asset:v1:" + compute_content_hash(
        canonical_json(
            {
                "candidate_id": candidate.id,
                "candidate_signature": candidate.signature,
                "content_hash": content_hash,
                "created_by": created_by,
                "definition": data,
            }
        )
    )
    asset = sign_asset(
        Asset(
            id=asset_id,
            name=name,
            content=content,
            content_type=str(data.get("content_type", "text")),
            created_by=created_by,
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
    graph_record = legacy_asset_graph_record(
        asset, domain_id=candidate.domain_id, causal_parent=receipt_id
    )
    return EffectProjection(
        records=(record, graph_record),
        relation_target=asset.id,
        assets=(asset,),
        accepted_asset_names=(asset.name,),
        additional_capabilities=(
            ("asset.publish.protected",) if prefix is not None else ()
        ),
    )


def _authorized_graph_key(
    actor_id: str, key_id: str, context: EffectProjectionContext
) -> ActorKey:
    matches = tuple(
        key
        for key in context.actor_keys
        if key.actor_id == actor_id and key.key_id == key_id
    )
    if len(matches) != 1:
        raise ValueError("asset graph object references an unauthorized actor key")
    return matches[0]


def project_content_publication(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    del candidate, context
    value = effect.payload.get("content")
    if not isinstance(value, Mapping):
        raise ValueError("asset.content.publish requires object payload.content")
    content = content_object_from_dict(value)
    validate_content_object(content)
    record = create_runtime_record(
        "asset.content.published",
        {"content": content_object_to_dict(content)},
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=content.id)


def project_definition_publication(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    value = effect.payload.get("definition")
    if not isinstance(value, Mapping):
        raise ValueError("asset.definition.publish requires object payload.definition")
    definition = signed_definition_from_dict(value)
    if definition.domain_id != candidate.domain_id:
        raise ValueError("definition domain does not match Candidate domain")
    key = _authorized_graph_key(definition.actor_id, definition.key_id, context)
    verify_signed_definition(definition, key)
    record = create_runtime_record(
        "asset.definition.published",
        {"definition": signed_definition_to_dict(definition)},
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=definition.id)


def project_definition_content_assertion(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    value = effect.payload.get("assertion")
    if not isinstance(value, Mapping):
        raise ValueError("asset.assert requires object payload.assertion")
    assertion = definition_content_assertion_from_dict(value)
    if assertion.domain_id != candidate.domain_id:
        raise ValueError("assertion domain does not match Candidate domain")
    key = _authorized_graph_key(assertion.actor_id, assertion.key_id, context)
    verify_definition_content_assertion(assertion, key)
    record = create_runtime_record(
        "asset.definition-content.asserted",
        {"assertion": definition_content_assertion_to_dict(assertion)},
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=assertion.id)


def project_worker_registration(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    del context
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
        actor_id=str(data.get("actor_id", "")),
        key_id=str(data.get("key_id", "")),
    )
    if not registration.actor_id or not registration.key_id:
        raise ValueError("worker.register requires actor_id and key_id binding")
    if registration.worker_id != registration.actor_id:
        raise ValueError("worker.register worker_id must equal its actor_id")
    record = create_runtime_record(
        "worker.registered",
        {
            **worker_registration_payload(registration),
            "registered_by": candidate.actor_id,
        },
        causal_parents=(receipt_id,),
    )
    return EffectProjection(
        records=(record,),
        relation_target=registration.worker_id,
    )


def project_replacement_claim(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    del context
    value = effect.payload.get("claim")
    if not isinstance(value, Mapping):
        raise ValueError("asset.relate requires an object payload.claim")
    data = deep_thaw(value)
    source_asset_id = str(data.get("source_asset_id", ""))
    replacement_asset_id = str(data.get("replacement_asset_id", ""))
    claim_type = str(data.get("claim_type", "replacement"))
    if not source_asset_id or not replacement_asset_id:
        raise ValueError("asset.relate requires both asset identifiers")
    derivation_version = str(data.get("derivation_version", ""))
    range_spec = str(data.get("range_spec", ""))
    claim = ReplacementClaim(
        id=hash_claim(
            source_asset_id,
            replacement_asset_id,
            claim_type,
            derivation_version=derivation_version,
            range_spec=range_spec,
        ),
        source_asset_id=source_asset_id,
        replacement_asset_id=replacement_asset_id,
        definition_hash=str(data.get("definition_hash", "")),
        claim_type=claim_type,
        signed_by=candidate.actor_id,
        provenance_seal=candidate.signature,
        lineage_id=str(data.get("lineage_id", "")),
        derivation_version=derivation_version,
        range_spec=range_spec,
    )
    record = create_runtime_record(
        "replacement.claimed",
        {"claim": replacement_claim_payload(claim)},
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=claim.id)


def project_contract_cancellation(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    del context
    contract_id = str(effect.payload.get("contract_id", ""))
    reason = str(effect.payload.get("reason", ""))
    if not contract_id or not reason:
        raise ValueError("contract.cancel requires contract_id and reason")
    record = create_terminal_record(
        contract_id,
        "cancelled",
        actor_id=candidate.actor_id,
        reason=reason,
        causal_parents=(receipt_id,),
    )
    return EffectProjection(records=(record,), relation_target=contract_id)


def project_actor_authorization(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    del context
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
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    del context
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


def project_actor_rotation(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    del context
    current_key_id = str(effect.payload.get("current_key_id", ""))
    reason = str(effect.payload.get("reason", ""))
    value = effect.payload.get("replacement_key")
    if not current_key_id or not reason or not isinstance(value, Mapping):
        raise ValueError(
            "actor.rotate requires current_key_id, replacement_key, and reason"
        )
    data = deep_thaw(value)
    replacement = ActorKey(
        actor_id=str(data.get("actor_id", "")),
        key_id=str(data.get("key_id", "")),
        kind=str(data.get("kind", "")),
        public_key=str(data.get("public_key", "")),
        capabilities=tuple(data.get("capabilities", ())),
    )
    if replacement.actor_id != candidate.actor_id:
        raise ValueError("actor.rotate can only rotate the signing actor's own key")
    if current_key_id != candidate.key_id:
        raise ValueError("actor.rotate current_key_id must match the signing key")
    if replacement.key_id == current_key_id:
        raise ValueError("actor.rotate replacement_key must use a new key_id")
    if replacement.kind in {"deterministic", "asig_"}:
        raise ValueError("actor rotation requires an authenticating key kind")
    authorized = create_runtime_record(
        "actor.authorized",
        {**actor_key_payload(replacement), "authorized_by": candidate.actor_id},
        causal_parents=(receipt_id,),
    )
    revoked = create_runtime_record(
        "actor.revoked",
        {
            "actor_id": candidate.actor_id,
            "key_id": current_key_id,
            "reason": reason,
            "revoked_by": candidate.actor_id,
        },
        causal_parents=(receipt_id, authorized.id),
    )
    return EffectProjection(
        records=(authorized, revoked),
        relation_target=f"{replacement.actor_id}/{replacement.key_id}",
        additional_capabilities=replacement.capabilities,
    )


def project_asset_attestation(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    projection = project_asset_attestation_records(
        effect, candidate, receipt_id, context
    )
    return EffectProjection(
        records=projection.records,
        relation_target=projection.relation_target,
        additional_capabilities=projection.additional_capabilities,
    )


EffectProjector = Callable[
    [CandidateEffect, CandidateProposal, str, EffectProjectionContext],
    EffectProjection,
]
BUILTIN_EFFECTS: Mapping[str, tuple[str, EffectProjector]] = MappingProxyType(
    {
        "asset.propose": ("asset.publish", project_asset_proposal),
        "asset.content.publish": ("asset.publish", project_content_publication),
        "asset.definition.publish": (
            "asset.publish",
            project_definition_publication,
        ),
        "asset.assert": ("asset.relate", project_definition_content_assertion),
        "contract.declare": ("contract.publish", project_contract_declaration),
        "worker.register": ("worker.register", project_worker_registration),
        "asset.relate": ("asset.relate", project_replacement_claim),
        "contract.cancel": ("contract.cancel", project_contract_cancellation),
        "actor.authorize": ("actor.authorize", project_actor_authorization),
        "actor.revoke": ("actor.revoke", project_actor_revocation),
        "actor.rotate": ("actor.rotate", project_actor_rotation),
        "asset.attest": ("asset.attest", project_asset_attestation),
    }
)
