"""Pure exact receipt, head and member proofs over a captured record history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from aigineering.protocol.runtime_record import RuntimeRecord, validate_runtime_record


def read_effect_types(payload):
    """Read the exact ordered effect sequence from a publication payload.

    This validates the container shape only. Feature owners remain responsible
    for comparing the returned values with their expected effects.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("publication effect types are not a sequence")
    effect_types = payload.get("effect_types")
    if not isinstance(effect_types, (tuple, list)) or any(
        not isinstance(effect_type, str) for effect_type in effect_types
    ):
        raise ValueError("publication effect types are not a sequence")
    return tuple(effect_types)


@dataclass(frozen=True)
class _PublishedRecordIndex:
    records: tuple
    by_id: Mapping
    by_type_candidate: Mapping


def index_published_records(records):
    """Build immutable exact lookups for one captured publication history."""
    rows = tuple(records)
    by_id: dict[str, list[RuntimeRecord]] = {}
    by_type_candidate: dict[tuple[str, str], list[RuntimeRecord]] = {}
    for row in rows:
        record_id = getattr(row, "id", None)
        if isinstance(record_id, str):
            by_id.setdefault(record_id, []).append(row)
        record_type = getattr(row, "record_type", None)
        payload = getattr(row, "payload", None)
        candidate_id = (
            payload.get("candidate_id") if isinstance(payload, Mapping) else None
        )
        if isinstance(record_type, str) and isinstance(candidate_id, str):
            by_type_candidate.setdefault((record_type, candidate_id), []).append(row)
    return _PublishedRecordIndex(
        records=rows,
        by_id=MappingProxyType({key: tuple(value) for key, value in by_id.items()}),
        by_type_candidate=MappingProxyType(
            {key: tuple(value) for key, value in by_type_candidate.items()}
        ),
    )


def validate_published_member(
    records,
    member,
    *,
    expected_candidate=None,
    expected_receipt=None,
    expected_head=None,
    domain_id=None,
    allow_additional_causal_parents=False,
    record_index=None,
):
    """Prove a member's exact received Candidate, unique head and membership."""
    rows = tuple(records)
    index = index_published_records(rows) if record_index is None else record_index
    if not isinstance(index, _PublishedRecordIndex) or (
        rows is not index.records
        and (
            len(index.records) != len(rows)
            or any(
                indexed is not captured
                for indexed, captured in zip(index.records, rows)
            )
        )
    ):
        raise ValueError("publication index does not match captured record history")
    _valid_record(member)
    member_parents = tuple(member.causal_parents)
    if not member_parents or (
        not allow_additional_causal_parents and len(member_parents) != 1
    ):
        raise ValueError("published member causal receipt shape is unsupported")
    if allow_additional_causal_parents:
        candidate_id = member.payload.get("candidate_id")
        receipt_matches = index.by_type_candidate.get(
            ("candidate.received", candidate_id), ()
        )
    else:
        receipt_matches = index.by_id.get(member_parents[0], ())
        receipt_matches = tuple(
            row for row in receipt_matches if row.record_type == "candidate.received"
        )
    if len(receipt_matches) != 1:
        raise ValueError("published member has no unique receipt")
    receipt = receipt_matches[0]
    candidate_id = receipt.payload.get("candidate_id")
    if receipt.id not in member_parents:
        raise ValueError("published member does not reference its receipt")
    if (
        member.payload.get("candidate_id", candidate_id) != candidate_id
        or member.payload.get("actor_id", receipt.payload.get("actor_id"))
        != receipt.payload.get("actor_id")
        or member.payload.get("key_id", receipt.payload.get("key_id"))
        != receipt.payload.get("key_id")
    ):
        raise ValueError("published member actor or Candidate witness changed")
    if domain_id is not None and receipt.payload.get("domain_id") != domain_id:
        raise ValueError("published receipt belongs to another domain")
    if expected_candidate is not None and candidate_id != expected_candidate:
        raise ValueError("published member Candidate witness changed")
    heads = index.by_type_candidate.get(("candidate.committed", candidate_id), ())
    if len(heads) != 1:
        raise ValueError("published member has no unique committed head")
    head = heads[0]
    if expected_receipt is not None and receipt.id != expected_receipt:
        raise ValueError("published receipt witness changed")
    if expected_head is not None and head.id != expected_head:
        raise ValueError("published head witness changed")
    ids = head.payload.get("committed_record_ids")
    if (
        not isinstance(ids, (tuple, list))
        or len(ids) != len(set(ids))
        or member.id not in ids
        or tuple(head.causal_parents) != (receipt.id, *tuple(ids))
        or receipt.id not in head.causal_parents
        or member.id not in head.causal_parents
    ):
        raise ValueError("published member is not in the exact committed head")
    _valid_record(receipt)
    _valid_record(head)
    for record_id in ids:
        committed_members = index.by_id.get(record_id, ())
        if len(committed_members) != 1:
            raise ValueError("committed head member is missing or ambiguous")
        _valid_record(committed_members[0])
    return receipt, head


def _valid_record(record):
    validate_runtime_record(record)
