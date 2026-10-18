"""Exact accepted continuation history for stateless completion repair."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from aigineering.core.causal_allowance import project_contract_allowance_records
from aigineering.core.effect_projectors import project_contract_declaration
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.publication_history import (
    index_published_records,
    read_effect_types,
    validate_published_member,
)
from aigineering.protocol.runtime_record import runtime_record_effective_payload
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.candidate import (
    CANDIDATE_PROTOCOL_VERSION,
    CandidateProposal,
    candidate_content_id,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_to_dict
from aigineering.task_view_query import (
    TaskViewQueryMismatch,
    load_task_view_query_context,
)
from aigineering.plugins.task_semantics import continuation_contract


class ContinuationHistoryCaptureUnstable(RuntimeError):
    """The Store changed on every bounded immutable read attempt."""


@dataclass(frozen=True)
class ContinuationHistorySnapshot:
    context: EffectProjectionContext
    store_domain_id: str
    publisher_domain_id: str
    publisher_actor_id: str


@dataclass(frozen=True)
class ContinuationPublicationProof:
    status: str
    reason: str = ""
    contract: Contract | None = None
    source_prefix: tuple = ()


def capture_continuation_history(store, publisher) -> ContinuationHistorySnapshot:
    """Capture one immutable Store history and explicit current publisher identity."""
    for _attempt in range(3):
        before = store.get_runtime_revision()
        try:
            query_context = load_task_view_query_context(store)
        except TaskViewQueryMismatch:
            continue
        after = store.get_runtime_revision()
        if before == after == query_context.revision:
            return ContinuationHistorySnapshot(
                context=query_context.effect_projection,
                store_domain_id=query_context.domain_id or "",
                publisher_domain_id=publisher.genesis.id,
                publisher_actor_id=publisher.actor_key.actor_id,
            )
    raise ContinuationHistoryCaptureUnstable(
        "continuation history changed during bounded capture"
    )


def inspect_continuation_history(
    snapshot: ContinuationHistorySnapshot,
    *,
    parent: Contract,
    source: Contract,
    method: str,
) -> ContinuationPublicationProof:
    """Prove the source declaration cut and one prior accepted child, if present."""
    if (
        snapshot.store_domain_id != snapshot.publisher_domain_id
        or not snapshot.publisher_actor_id
    ):
        return _conflict("publisher_identity_mismatch")
    context = snapshot.context
    rows = context.runtime_records
    source_rows = tuple(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and isinstance(row.payload, Mapping)
        and isinstance(row.payload.get("contract"), Mapping)
        and row.payload["contract"].get("id") == source.id
    )
    contracts_by_id: dict[str, list[Contract]] = {}
    for contract in context.contracts:
        contracts_by_id.setdefault(contract.id, []).append(contract)
    source_contracts = tuple(contracts_by_id.get(source.id, ()))
    parents = tuple(contracts_by_id.get(parent.id, ()))
    if (
        len(source_rows) != 1
        or len(source_contracts) != 1
        or len(parents) != 1
        or source.parent_id != parent.id
        or contract_to_dict(source_contracts[0]) != contract_to_dict(source)
        or contract_to_dict(parents[0]) != contract_to_dict(parent)
        or deep_thaw(source_rows[0].payload.get("contract"))
        != deep_thaw(contract_to_dict(source))
    ):
        return _conflict("source_declaration_missing_or_ambiguous")
    source_member = source_rows[0]
    source_candidate_id = source_member.payload.get("candidate_id")
    if not isinstance(source_candidate_id, str) or not source_candidate_id:
        return _conflict("source_candidate_missing")
    index = index_published_records(rows)
    try:
        _source_receipt, source_head = validate_published_member(
            rows,
            source_member,
            expected_candidate=source_candidate_id,
            domain_id=snapshot.store_domain_id,
            record_index=index,
        )
    except (TypeError, ValueError):
        return _conflict("source_declaration_publication_invalid")
    positions = {row.id: position for position, row in enumerate(rows)}
    if len(positions) != len(rows):
        return _conflict("source_history_record_id_ambiguous")
    head_position = positions.get(source_head.id)
    if head_position is None:
        return _conflict("source_declaration_head_missing")
    member_ids = source_head.payload.get("committed_record_ids")
    if not isinstance(member_ids, (tuple, list)) or any(
        positions.get(record_id, head_position) >= head_position
        for record_id in member_ids
    ):
        return _conflict("source_declaration_head_incomplete")
    source_prefix = rows[: head_position + 1]

    child_rows = tuple(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and isinstance(row.payload, Mapping)
        and isinstance(row.payload.get("contract"), Mapping)
        and row.payload["contract"].get("parent_id") == parent.id
        and row.payload["contract"].get("origin") == "continuation"
    )
    child_receipts = tuple(
        row
        for row in rows
        if row.record_type == "candidate.received"
        and tuple(row.causal_parents) == (source.id,)
        and row.payload.get("domain_id") == snapshot.store_domain_id
        and row.payload.get("actor_id") == snapshot.publisher_actor_id
    )
    if any(
        row.payload.get("candidate_id")
        not in {child.payload.get("candidate_id") for child in child_rows}
        and _is_sole_declaration(row)
        for row in child_receipts
    ):
        return _conflict("source_continuation_member_missing")

    candidates: list[tuple[Contract, object]] = []
    for member in child_rows:
        raw_contract = member.payload["contract"]
        candidate_id = member.payload.get("candidate_id")
        raw_budget = raw_contract.get("budget")
        canonical = (
            continuation_contract(parent, source, method=method, budget=raw_budget)
            if type(raw_budget) is int and raw_budget > 0
            else None
        )
        canonical_payload = (
            deep_thaw(contract_to_dict(canonical)) if canonical is not None else None
        )
        canonical_member = canonical_payload == deep_thaw(raw_contract)
        source_receipt = any(
            row.payload.get("candidate_id") == candidate_id
            and _is_sole_declaration(row)
            for row in child_receipts
        )
        if not canonical_member and not source_receipt:
            continue
        if canonical is None or not canonical_member:
            return _conflict("continuation_contract_or_source_mismatch")

        contract_id = raw_contract.get("id")
        materialized = tuple(contracts_by_id.get(contract_id, ()))
        if len(materialized) != 1:
            return _conflict("continuation_materialized_contract_missing_or_ambiguous")
        child = materialized[0]
        if deep_thaw(contract_to_dict(child)) != canonical_payload:
            return _conflict("continuation_materialized_contract_mismatch")

        receipt_ids = tuple(
            parent_id
            for parent_id in member.causal_parents
            if index.by_id.get(parent_id)
            and any(
                row.record_type == "candidate.received"
                for row in index.by_id[parent_id]
            )
        )
        if len(receipt_ids) != 1:
            return _conflict("continuation_receipt_missing_or_ambiguous")
        receipt_matches = tuple(
            row
            for row in index.by_id[receipt_ids[0]]
            if row.record_type == "candidate.received"
        )
        if len(receipt_matches) != 1:
            return _conflict("continuation_receipt_missing_or_ambiguous")
        receipt = receipt_matches[0]
        if (
            receipt.payload.get("candidate_id") != candidate_id
            or tuple(receipt.causal_parents) != (source.id,)
            or receipt.payload.get("domain_id") != snapshot.store_domain_id
            or receipt.payload.get("actor_id") != snapshot.publisher_actor_id
        ):
            return _conflict("continuation_contract_or_source_mismatch")
        candidates.append((canonical, member))
    if len(candidates) > 1:
        return _conflict("continuation_publication_ambiguous")
    if not candidates:
        return ContinuationPublicationProof("absent", source_prefix=source_prefix)
    child, member = candidates[0]
    proof = _prove_child_publication(
        snapshot,
        child=child,
        member=member,
        source=source,
        index=index,
        contracts_by_id=contracts_by_id,
    )
    if proof.status != "matched":
        return proof
    return replace(proof, source_prefix=source_prefix)


def _prove_child_publication(
    snapshot: ContinuationHistorySnapshot,
    *,
    child: Contract,
    member,
    source: Contract,
    index,
    contracts_by_id,
) -> ContinuationPublicationProof:
    rows = snapshot.context.runtime_records
    candidate_id = member.payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return _conflict("continuation_candidate_missing")
    receipt_rows = index.by_type_candidate.get(("candidate.received", candidate_id), ())
    if len(receipt_rows) != 1:
        return _conflict("continuation_receipt_missing_or_ambiguous")
    receipt = receipt_rows[0]
    payload = receipt.payload
    if (
        payload.get("domain_id") != snapshot.store_domain_id
        or payload.get("actor_id") != snapshot.publisher_actor_id
        or tuple(receipt.causal_parents) != (source.id,)
        or payload.get("metadata") != {}
    ):
        return _conflict("continuation_receipt_identity_or_intent_mismatch")
    try:
        effect_types = read_effect_types(payload)
    except ValueError:
        return _conflict("continuation_effect_types_malformed")
    if effect_types != ("contract.declare",):
        return _conflict("continuation_effect_types_mismatch")
    key = f"continuation:{source.id}:{child.id}"
    try:
        candidate = _reconstruct_candidate(receipt, child, key)
    except (TypeError, ValueError):
        return _conflict("continuation_candidate_reconstruction_failed")
    if candidate.id != candidate_id:
        return _conflict("continuation_candidate_intent_mismatch")
    effect = contract_declaration_effect(child)
    try:
        _receipt, head = validate_published_member(
            rows,
            member,
            expected_candidate=candidate_id,
            expected_receipt=receipt.id,
            domain_id=snapshot.store_domain_id,
            record_index=index,
        )
        contract_projection = project_contract_declaration(
            effect, candidate, receipt.id, snapshot.context
        )
    except (TypeError, ValueError):
        return _conflict("continuation_publication_history_invalid")
    if len(contract_projection.records) != 1:
        return _conflict("continuation_contract_projection_incomplete")
    projected_contract = contract_projection.records[0]
    receipt_position = next(
        position for position, row in enumerate(rows) if row.id == receipt.id
    )
    prefix = rows[:receipt_position]
    declared_before_receipt = {
        row.payload["contract"]["id"]
        for row in prefix
        if row.record_type == "contract.declared"
        and isinstance(row.payload, Mapping)
        and isinstance(row.payload.get("contract"), Mapping)
        and isinstance(row.payload["contract"].get("id"), str)
    }
    existing_contracts = tuple(
        contract
        for contract in snapshot.context.contracts
        if contract.id != child.id and contract.id in declared_before_receipt
    )
    try:
        allowance_records = project_contract_allowance_records(
            (child,), existing_contracts, prefix, causal_parent=receipt.id
        )
    except (TypeError, ValueError):
        return _conflict("continuation_allowance_projection_invalid")
    expected_members = (projected_contract, *allowance_records)
    head_ids = head.payload.get("committed_record_ids")
    if not isinstance(head_ids, (tuple, list)) or tuple(head_ids) != tuple(
        record.id for record in expected_members
    ):
        return _conflict("continuation_committed_batch_mismatch")
    for expected in expected_members:
        actual_rows = index.by_id.get(expected.id, ())
        if len(actual_rows) != 1 or runtime_record_effective_payload(
            actual_rows[0]
        ) != runtime_record_effective_payload(expected):
            return _conflict("continuation_committed_member_mismatch")
    matching_contracts = tuple(contracts_by_id.get(child.id, ()))
    if (
        member.record_type != projected_contract.record_type
        or member.id != projected_contract.id
        or member.payload != projected_contract.payload
        or tuple(member.causal_parents) != tuple(projected_contract.causal_parents)
        or len(matching_contracts) != 1
        or contract_to_dict(matching_contracts[0]) != contract_to_dict(child)
    ):
        return _conflict("continuation_materialized_contract_mismatch")
    return ContinuationPublicationProof("matched", contract=child)


def _reconstruct_candidate(receipt, child: Contract, key: str) -> CandidateProposal:
    payload = receipt.payload
    values = tuple(
        payload.get(field)
        for field in (
            "candidate_id",
            "actor_id",
            "key_id",
            "signature_kind",
            "signature",
        )
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("continuation receipt identity is malformed")
    if (
        payload.get("protocol_version", CANDIDATE_PROTOCOL_VERSION)
        != CANDIDATE_PROTOCOL_VERSION
    ):
        raise ValueError("continuation receipt protocol version is unsupported")
    if payload.get("claim_binding") is not None:
        raise ValueError("continuation publication cannot carry a claim binding")
    candidate = CandidateProposal(
        id=payload["candidate_id"],
        domain_id=payload["domain_id"],
        actor_id=payload["actor_id"],
        key_id=payload["key_id"],
        signature_kind=payload["signature_kind"],
        signature=payload["signature"],
        effects=(contract_declaration_effect(child),),
        causal_parents=tuple(receipt.causal_parents),
        idempotency_key=key,
        metadata={},
        protocol_version=CANDIDATE_PROTOCOL_VERSION,
    )
    return replace(candidate, id=candidate_content_id(candidate))


def _is_sole_declaration(receipt) -> bool:
    try:
        return read_effect_types(receipt.payload) == ("contract.declare",)
    except (TypeError, ValueError):
        return False


def _conflict(reason: str) -> ContinuationPublicationProof:
    return ContinuationPublicationProof("conflict", reason=reason)
