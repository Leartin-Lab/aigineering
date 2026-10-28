"""Exact committed-head recognition for strict injected fact projectors.

This module recognizes only a byte-for-byte semantic replay of one already
committed, single-effect Candidate. It does not make a projection or a handler
authoritative; callers still run all normal effect and writer validation.
"""

from __future__ import annotations

from collections import Counter

from aigineering.protocol.candidate import CandidateProposal
from aigineering.protocol.runtime_record import RuntimeRecord, validate_runtime_record


# These are facts added by generic projection after an effect projector returns
# its direct records. Feature-owned records must be listed by their handler.
CORE_GENERATED_FACT_TYPES = frozenset(
    {
        "allowance.granted",
        "allowance.reserved",
        "asset.committed",
    }
)


def exact_single_effect_head_replay(
    candidate: CandidateProposal,
    receipt_id: str,
    projected_records: tuple[RuntimeRecord, ...],
    runtime_records: tuple[RuntimeRecord, ...],
) -> bool:
    """Return whether records exactly reproduce this Candidate's full head.

    A matching candidate id alone is insufficient. The authenticated receipt,
    effect list, committed head, ordered membership, and every immutable record
    must all agree.
    """
    if len(candidate.effects) != 1:
        return False

    by_id: dict[str, list[RuntimeRecord]] = {}
    for record in runtime_records:
        by_id.setdefault(record.id, []).append(record)

    receipts = [
        record
        for record in runtime_records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
    ]
    if len(receipts) != 1:
        return False
    receipt = receipts[0]
    if receipt.id != receipt_id:
        return False
    try:
        validate_runtime_record(receipt)
    except (TypeError, ValueError):
        return False
    if (
        receipt.payload.get("actor_id") != candidate.actor_id
        or receipt.payload.get("key_id") != candidate.key_id
        or tuple(receipt.payload.get("effect_types", ()))
        != tuple(effect.effect_type for effect in candidate.effects)
    ):
        return False

    heads = [
        record
        for record in runtime_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
    ]
    if len(heads) != 1:
        return False
    head = heads[0]
    try:
        validate_runtime_record(head)
    except (TypeError, ValueError):
        return False
    member_ids = head.payload.get("committed_record_ids")
    if not isinstance(member_ids, (tuple, list)) or any(
        not isinstance(member_id, str) or not member_id for member_id in member_ids
    ):
        return False
    ordered_ids = tuple(member_ids)
    if len(ordered_ids) != len(set(ordered_ids)):
        return False
    if head.causal_parents != (receipt.id, *ordered_ids):
        return False
    if Counter(record.id for record in projected_records) != Counter(ordered_ids):
        return False
    if tuple(record.id for record in projected_records) != ordered_ids:
        return False

    for projected, member_id in zip(projected_records, ordered_ids, strict=True):
        matches = by_id.get(member_id, ())
        if len(matches) != 1:
            return False
        committed_member = matches[0]
        try:
            validate_runtime_record(committed_member)
        except (TypeError, ValueError):
            return False
        if projected != committed_member:
            return False
    return True
