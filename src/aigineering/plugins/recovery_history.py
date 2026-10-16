"""Exact historical proof for already accepted recovery publications."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from aigineering.core.effect_projectors import (
    project_asset_proposal,
    project_contract_declaration,
)
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.publication_history import (
    index_published_records,
    read_effect_types,
    validate_published_member,
)
from aigineering.protocol.candidate import (
    CANDIDATE_PROTOCOL_VERSION,
    CandidateEffect,
    CandidateProposal,
    candidate_content_id,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict, contract_to_dict


@dataclass(frozen=True)
class RecoveryHistorySnapshot:
    """Immutable fact capture plus public configured publisher identity."""

    context: EffectProjectionContext
    store_domain_id: str
    publisher_domain_id: str
    publisher_actor_id: str
    revision: int


class RecoveryHistoryCaptureUnstable(RuntimeError):
    """The Store revision changed on every bounded immutable read attempt."""


@dataclass(frozen=True)
class RecoveryPublicationProof:
    """Exact existing-publication result for one deterministic recovery intent."""

    status: Literal["absent", "matched", "conflict"]
    reason: str = ""
    contract: Contract | None = None
    context_asset: Asset | None = None


def prove_recovery_publication(
    snapshot: RecoveryHistorySnapshot,
    *,
    recovery: Contract,
    context_template: Asset,
    effects: tuple[CandidateEffect, ...],
    idempotency_key: str,
    causal_parents: tuple[str, ...],
) -> RecoveryPublicationProof:
    """Prove one exact accepted recovery Candidate and its materialized members.

    The exact deterministic Contract declaration anchors recovery history.
    A same-name Asset is never sufficient: after rebuilding the Candidate from
    the accepted declaration's receipt, project the expected Asset ID and
    select that member from the same committed head.
    """

    if snapshot.store_domain_id != snapshot.publisher_domain_id:
        return _conflict("publisher_domain_mismatch")
    if len(effects) != 2 or tuple(effect.effect_type for effect in effects) != (
        "contract.declare",
        "asset.propose",
    ):
        return _conflict("unexpected_recovery_effects")
    if effects != (
        contract_declaration_effect(recovery),
        asset_proposal_effect(context_template),
    ):
        return _conflict("recovery_effect_intent_mismatch")

    rows = snapshot.context.runtime_records
    contract_rows = tuple(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and isinstance(row.payload, Mapping)
        and isinstance(row.payload.get("contract"), Mapping)
        and row.payload["contract"].get("id") == recovery.id
    )
    contracts = tuple(
        contract
        for contract in snapshot.context.contracts
        if contract.id == recovery.id
    )
    if not contract_rows and not contracts:
        return _inspect_unanchored_context_history(
            snapshot,
            context_template=context_template,
            effects=effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )
    if len(contract_rows) != 1 or len(contracts) != 1:
        return _conflict("recovery_contract_missing_or_ambiguous")

    contract_member = contract_rows[0]
    candidate_id = contract_member.payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return _conflict("recovery_contract_candidate_missing")
    receipts = tuple(
        row
        for row in rows
        if row.record_type == "candidate.received"
        and isinstance(row.payload, Mapping)
        and row.payload.get("candidate_id") == candidate_id
    )
    if len(receipts) != 1:
        return _conflict("recovery_receipt_missing_or_ambiguous")
    receipt = receipts[0]
    receipt_payload = receipt.payload
    if (
        receipt_payload.get("domain_id") != snapshot.store_domain_id
        or receipt_payload.get("actor_id") != snapshot.publisher_actor_id
        or tuple(receipt.causal_parents) != causal_parents
        or receipt_payload.get("metadata") != {}
    ):
        return _conflict("recovery_receipt_intent_mismatch")
    try:
        effect_types = read_effect_types(receipt_payload)
    except ValueError:
        return _conflict("recovery_effect_types_malformed")
    if effect_types != ("contract.declare", "asset.propose"):
        return _conflict("recovery_effect_types_mismatch")

    actor_id = receipt_payload.get("actor_id")
    key_id = receipt_payload.get("key_id")
    signature_kind = receipt_payload.get("signature_kind")
    signature = receipt_payload.get("signature")
    if not all(
        isinstance(value, str) and value
        for value in (actor_id, key_id, signature_kind, signature)
    ):
        return _conflict("recovery_receipt_identity_malformed")
    try:
        candidate, expected_candidate_id = _reconstruct_expected_candidate(
            snapshot,
            receipt,
            effects=effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )
    except (TypeError, ValueError):
        return _conflict("recovery_candidate_reconstruction_failed")
    if expected_candidate_id != candidate_id:
        return _conflict("recovery_candidate_intent_mismatch")

    index = index_published_records(rows)
    try:
        contract_receipt, contract_head = validate_published_member(
            rows,
            contract_member,
            expected_candidate=candidate_id,
            expected_receipt=receipt.id,
            domain_id=snapshot.store_domain_id,
            record_index=index,
        )
        contract_projection = project_contract_declaration(
            effects[0], candidate, receipt.id, snapshot.context
        )
        asset_projection = project_asset_proposal(
            effects[1], candidate, receipt.id, snapshot.context
        )
    except (TypeError, ValueError):
        return _conflict("recovery_publication_projection_or_history_invalid")
    if len(contract_projection.records) != 1:
        return _conflict("recovery_contract_projection_incomplete")
    expected_contract_record = contract_projection.records[0]
    if (
        contract_member.id != expected_contract_record.id
        or contract_member.record_type != expected_contract_record.record_type
        or contract_member.payload != expected_contract_record.payload
        or tuple(contract_member.causal_parents)
        != tuple(expected_contract_record.causal_parents)
        or contract_member.payload
        != {"candidate_id": candidate_id, "contract": contract_to_dict(recovery)}
        or contract_to_dict(contracts[0]) != contract_to_dict(recovery)
    ):
        return _conflict("recovery_contract_member_or_materialization_mismatch")

    if len(asset_projection.assets) != 1:
        return _conflict("recovery_context_projection_incomplete")
    expected_asset = asset_projection.assets[0]
    expected_asset_records = tuple(
        record
        for record in asset_projection.records
        if record.record_type == "asset.committed"
    )
    if len(expected_asset_records) != 1:
        return _conflict("recovery_context_projection_incomplete")
    expected_asset_record = expected_asset_records[0]
    asset_members = index.by_id.get(expected_asset_record.id, ())
    assets = tuple(
        asset for asset in snapshot.context.assets if asset.id == expected_asset.id
    )
    if len(asset_members) != 1 or len(assets) != 1:
        return _conflict("recovery_context_member_missing_or_ambiguous")
    asset_member = asset_members[0]
    try:
        asset_receipt, asset_head = validate_published_member(
            rows,
            asset_member,
            expected_candidate=candidate_id,
            expected_receipt=receipt.id,
            expected_head=contract_head.id,
            domain_id=snapshot.store_domain_id,
            record_index=index,
        )
    except (TypeError, ValueError):
        return _conflict("recovery_context_history_invalid")
    if (
        contract_receipt.id != asset_receipt.id
        or contract_head.id != asset_head.id
        or asset_member.record_type != expected_asset_record.record_type
        or asset_member.payload != expected_asset_record.payload
        or tuple(asset_member.causal_parents)
        != tuple(expected_asset_record.causal_parents)
        or asset_to_dict(assets[0]) != asset_to_dict(expected_asset)
        or asset_member.payload.get("asset") != asset_to_dict(expected_asset)
    ):
        return _conflict("recovery_context_member_or_materialization_mismatch")
    return RecoveryPublicationProof(
        "matched", contract=contracts[0], context_asset=assets[0]
    )


def _reconstruct_expected_candidate(
    snapshot: RecoveryHistorySnapshot,
    receipt,
    *,
    effects: tuple[CandidateEffect, ...],
    idempotency_key: str,
    causal_parents: tuple[str, ...],
) -> tuple[CandidateProposal, str]:
    """Rebuild a candidate-shaped intent and return its canonical content ID."""
    payload = receipt.payload
    candidate_id = payload.get("candidate_id")
    values = (
        candidate_id,
        payload.get("actor_id"),
        payload.get("key_id"),
        payload.get("signature_kind"),
        payload.get("signature"),
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("recovery receipt identity is malformed")
    candidate = CandidateProposal(
        id=candidate_id,
        domain_id=snapshot.store_domain_id,
        actor_id=payload["actor_id"],
        key_id=payload["key_id"],
        signature_kind=payload["signature_kind"],
        signature=payload["signature"],
        effects=effects,
        causal_parents=causal_parents,
        idempotency_key=idempotency_key,
        metadata={},
        protocol_version=CANDIDATE_PROTOCOL_VERSION,
    )
    expected_candidate_id = candidate_content_id(candidate)
    return replace(candidate, id=expected_candidate_id), expected_candidate_id


def _inspect_unanchored_context_history(
    snapshot: RecoveryHistorySnapshot,
    *,
    context_template: Asset,
    effects: tuple[CandidateEffect, ...],
    idempotency_key: str,
    causal_parents: tuple[str, ...],
) -> RecoveryPublicationProof:
    """Ignore only complete generic publications with provably foreign intent."""
    rows = snapshot.context.runtime_records
    members = tuple(
        row
        for row in rows
        if row.record_type == "asset.committed"
        and isinstance(row.payload, Mapping)
        and isinstance(row.payload.get("asset"), Mapping)
        and row.payload["asset"].get("name") == context_template.name
    )
    assets = tuple(
        asset
        for asset in snapshot.context.assets
        if asset.name == context_template.name
    )
    if not members and not assets:
        return RecoveryPublicationProof("absent")
    if not members or not assets:
        return _conflict("unanchored_context_history_incomplete")

    index = index_published_records(rows)
    matched_materializations: set[str] = set()
    for member in members:
        member_asset = member.payload.get("asset")
        member_asset_id = member_asset.get("id")
        if not isinstance(member_asset_id, str) or not member_asset_id:
            return _conflict("unanchored_context_member_malformed")
        if len(tuple(member.causal_parents)) != 1:
            return _conflict("unanchored_context_member_receipt_invalid")
        receipt_rows = index.by_id.get(member.causal_parents[0], ())
        receipt_rows = tuple(
            row for row in receipt_rows if row.record_type == "candidate.received"
        )
        if len(receipt_rows) != 1:
            return _conflict("unanchored_context_receipt_missing_or_ambiguous")
        receipt = receipt_rows[0]
        try:
            expected_candidate, expected_candidate_id = _reconstruct_expected_candidate(
                snapshot,
                receipt,
                effects=effects,
                idempotency_key=idempotency_key,
                causal_parents=causal_parents,
            )
            expected_asset_projection = project_asset_proposal(
                effects[1], expected_candidate, receipt.id, snapshot.context
            )
        except (TypeError, ValueError):
            return _conflict("unanchored_context_intent_unreadable")
        if len(expected_asset_projection.assets) != 1:
            return _conflict("unanchored_context_projection_incomplete")
        expected_asset = expected_asset_projection.assets[0]
        if (
            expected_candidate_id == receipt.payload.get("candidate_id")
            or expected_asset.id == member_asset_id
        ):
            return _conflict("unanchored_exact_context_residue")

        try:
            validate_published_member(
                rows,
                member,
                domain_id=snapshot.store_domain_id,
                record_index=index,
            )
        except (TypeError, ValueError):
            return _conflict("unanchored_context_generic_history_invalid")
        materializations = tuple(
            asset for asset in assets if asset.id == member_asset_id
        )
        if (
            len(materializations) != 1
            or asset_to_dict(materializations[0]) != member_asset
        ):
            return _conflict("unanchored_context_materialization_invalid")
        matched_materializations.add(member_asset_id)

    if {asset.id for asset in assets} != matched_materializations:
        return _conflict("unanchored_context_materialization_unmatched")
    return RecoveryPublicationProof("absent")


def _conflict(reason: str) -> RecoveryPublicationProof:
    return RecoveryPublicationProof("conflict", reason=reason)
