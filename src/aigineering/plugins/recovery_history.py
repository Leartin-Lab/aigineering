"""Exact historical proof for already accepted recovery publications."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.publication_history import (
    index_published_records,
    read_effect_types,
    validate_published_member,
)
from aigineering.protocol.candidate import (
    CandidateEffect,
)
from aigineering.protocol.immutability import deep_thaw
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
    query_context: object | None = None


class RecoveryHistoryCaptureUnstable(RuntimeError):
    """The Store revision changed on every bounded immutable read attempt."""


def capture_recovery_history(store, publisher) -> RecoveryHistorySnapshot:
    """Capture exact recovery facts from a caller-owned immutable Store view.

    Callers that need a stable multi-reader proof pass their own SQLite snapshot;
    this function does not retain a live Store handle or create another state
    owner. The snapshot context is read-only projection input only.
    """
    from aigineering.core.domain import load_genesis
    from aigineering.task_view_query import load_task_view_query_context

    domain_id = load_genesis(store).id
    publisher_domain_id = getattr(getattr(publisher, "genesis", None), "id", None)
    actor_key = getattr(publisher, "actor_key", None)
    actor_id = getattr(actor_key, "actor_id", None)
    if not isinstance(domain_id, str) or not domain_id:
        raise ValueError("recovery publisher has no domain identity")
    if not isinstance(actor_id, str) or not actor_id:
        raise ValueError("recovery publisher has no Actor identity")
    if publisher_domain_id != domain_id:
        raise ValueError("recovery publisher belongs to another domain")
    query = load_task_view_query_context(store)
    return RecoveryHistorySnapshot(
        context=query.effect_projection,
        store_domain_id=domain_id,
        publisher_domain_id=publisher_domain_id,
        publisher_actor_id=actor_id,
        revision=store.get_runtime_revision(),
        query_context=query,
    )


@dataclass(frozen=True)
class RecoveryPublicationProof:
    """Exact existing-publication result for one deterministic recovery intent."""

    status: Literal["absent", "matched", "conflict"]
    reason: str = ""
    contract: Contract | None = None
    context_asset: Asset | None = None
    receipt_id: str | None = None


def prove_recovery_publication(
    snapshot: RecoveryHistorySnapshot,
    *,
    recovery: Contract,
    context_template: Asset,
    effects: tuple[CandidateEffect, ...],
    idempotency_key: str,
    causal_parents: tuple[str, ...],
    source_record_ids: tuple[str, ...] = (),
) -> RecoveryPublicationProof:
    """Read one already committed recovery from its actual receipt and head.

    The supplied Contract and Asset are intent hints only. Historical members
    are selected from the immutable head and validated against their
    materialized facts; no builder recreates a historical publication.
    """
    del context_template, effects
    if snapshot.store_domain_id != snapshot.publisher_domain_id:
        return _conflict("publisher_domain_mismatch")
    rows = snapshot.context.runtime_records
    if not isinstance(idempotency_key, str) or not idempotency_key:
        return _conflict("recovery_idempotency_key_invalid")
    positions = {row.id: index for index, row in enumerate(rows)}
    if len(positions) != len(rows):
        return _conflict("recovery_history_record_id_ambiguous")
    candidate_receipts = tuple(
        row
        for row in rows
        if row.record_type == "candidate.received"
        and isinstance(row.payload, Mapping)
        and row.payload.get("actor_id") == snapshot.publisher_actor_id
        and tuple(row.causal_parents) == causal_parents
    )
    if not candidate_receipts:
        anchored = any(
            row.record_type == "contract.declared"
            and isinstance(row.payload, Mapping)
            and isinstance(row.payload.get("contract"), Mapping)
            and row.payload["contract"].get("id") == recovery.id
            for row in rows
        )
        return (
            _conflict("recovery_receipt_missing")
            if anchored
            else RecoveryPublicationProof("absent")
        )
    if len(candidate_receipts) != 1:
        return _conflict("recovery_receipt_ambiguous")
    receipt = candidate_receipts[0]
    if tuple(receipt.causal_parents) != causal_parents:
        return _conflict("recovery_receipt_intent_mismatch")
    receipt_position = positions.get(receipt.id)
    if (
        receipt_position is None
        or any(
            source_id not in positions or positions[source_id] >= receipt_position
            for source_id in source_record_ids
        )
        or any(
            source_id not in receipt.causal_parents for source_id in source_record_ids
        )
    ):
        return _conflict("recovery_source_parent_missing")
    try:
        effect_types = read_effect_types(receipt.payload)
    except ValueError:
        return _conflict("recovery_effect_types_malformed")
    if effect_types != ("contract.declare", "asset.propose"):
        return _conflict("recovery_effect_types_mismatch")
    candidate_id = receipt.payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return _conflict("recovery_candidate_id_missing")
    heads = tuple(
        row
        for row in rows
        if row.record_type == "candidate.committed"
        and isinstance(row.payload, Mapping)
        and row.payload.get("candidate_id") == candidate_id
    )
    if len(heads) != 1:
        return _conflict("recovery_head_missing_or_ambiguous")
    head = heads[0]
    member_ids = tuple(head.payload.get("committed_record_ids", ()))
    if not member_ids or len(member_ids) != len(set(member_ids)):
        return _conflict("recovery_head_members_malformed")
    record_index = index_published_records(rows)
    committed = tuple(
        record
        for record_id in member_ids
        for record in record_index.by_id.get(record_id, ())
    )
    contract_members = tuple(
        record for record in committed if record.record_type == "contract.declared"
    )
    asset_members = tuple(
        record for record in committed if record.record_type == "asset.committed"
    )
    if len(contract_members) != 1 or len(asset_members) != 1:
        return _conflict("recovery_head_effect_members_invalid")
    contract_member, asset_member = contract_members[0], asset_members[0]
    try:
        contract_receipt, contract_head = validate_published_member(
            rows,
            contract_member,
            expected_candidate=candidate_id,
            expected_receipt=receipt.id,
            expected_head=head.id,
            domain_id=snapshot.store_domain_id,
            record_index=record_index,
        )
        asset_receipt, asset_head = validate_published_member(
            rows,
            asset_member,
            expected_candidate=candidate_id,
            expected_receipt=receipt.id,
            expected_head=head.id,
            domain_id=snapshot.store_domain_id,
            record_index=record_index,
        )
    except (TypeError, ValueError):
        return _conflict("recovery_member_history_invalid")
    if contract_receipt.id != asset_receipt.id or contract_head.id != asset_head.id:
        return _conflict("recovery_members_split_across_heads")
    contract_value = contract_member.payload.get("contract")
    asset_value = asset_member.payload.get("asset")
    if not isinstance(contract_value, Mapping) or not isinstance(asset_value, Mapping):
        return _conflict("recovery_member_payload_malformed")
    if contract_value.get("id") != recovery.id:
        return _conflict("recovery_contract_intent_mismatch")
    contracts = tuple(
        item for item in snapshot.context.contracts if item.id == recovery.id
    )
    assets = tuple(
        item for item in snapshot.context.assets if item.id == asset_value.get("id")
    )
    if len(contracts) != 1 or len(assets) != 1:
        return _conflict("recovery_materialization_missing_or_ambiguous")
    if deep_thaw(contract_member.payload.get("contract")) != deep_thaw(
        contract_to_dict(contracts[0])
    ) or deep_thaw(asset_value) != deep_thaw(asset_to_dict(assets[0])):
        return _conflict("recovery_materialization_mismatch")
    return RecoveryPublicationProof(
        "matched",
        contract=contracts[0],
        context_asset=assets[0],
        receipt_id=receipt.id,
    )


def _conflict(reason: str) -> RecoveryPublicationProof:
    return RecoveryPublicationProof("conflict", reason=reason)
