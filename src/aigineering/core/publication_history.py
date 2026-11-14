"""Pure exact receipt, head and member proofs over a captured record history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from aigineering.protocol.runtime_record import RuntimeRecord, validate_runtime_record


class CandidateHistoryError(ValueError):
    """Historical replay cannot prove a complete immutable decision."""


def committed_member_ids(head):
    """Read the versioned head format, including immutable legacy heads."""
    fields = {"candidate_id", "committed_record_ids"}
    if "closure_version" in head.payload:
        if (
            type(head.payload["closure_version"]) is not int
            or head.payload["closure_version"] != 1
        ):
            raise ValueError("unsupported Candidate closure version")
        fields.add("closure_version")
    ids = head.payload.get("committed_record_ids")
    if (
        set(head.payload) != fields
        or not isinstance(ids, (tuple, list))
        or any(not isinstance(item, str) or not item for item in ids)
        or len(ids) != len(set(ids))
    ):
        raise ValueError("Candidate committed head fields are invalid")
    return tuple(ids)


def candidate_commit_head(receipt, records):
    """Close one logical action only after all of its records are known."""
    from aigineering.protocol.runtime_record import create_runtime_record

    member_ids = tuple(row.id for row in records if row.id != receipt.id)
    if len(member_ids) != len(set(member_ids)):
        raise ValueError("Candidate closure contains duplicate members")
    return create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": receipt.payload["candidate_id"],
            "closure_version": 1,
            "committed_record_ids": member_ids,
        },
        causal_parents=(receipt.id, *member_ids),
    )


def committed_candidate_decision(receipt, records):
    """Return exact immutable history without running any effect maker."""
    from aigineering.core.candidate_decision import CommitmentDecision
    from aigineering.protocol.runtime_record import runtime_record_effective_payload
    from aigineering.protocol.wire import (
        asset_from_dict,
        contract_from_dict,
        trace_entry_from_dict,
    )

    candidate_id = receipt.payload["candidate_id"]
    heads = tuple(
        row
        for row in records
        if row.record_type == "candidate.committed"
        and row.payload.get("candidate_id") == candidate_id
    )
    receipts = tuple(
        row
        for row in records
        if row.record_type == "candidate.received"
        and row.payload.get("candidate_id") == candidate_id
    )
    if len(heads) != 1 or len(receipts) != 1:
        raise ValueError("Candidate history has no unique receipt and committed head")
    head, original = heads[0], receipts[0]
    if runtime_record_effective_payload(receipt) != runtime_record_effective_payload(
        original
    ):
        raise ValueError("Candidate replay differs from the authenticated receipt")
    ids = committed_member_ids(head)
    if head.payload.get("closure_version") != 1:
        raise CandidateHistoryError(
            "legacy Candidate head has no complete closure; inspect its history "
            "without replaying effects"
        )
    if (
        original.id in ids
        or head.id in ids
        or head.causal_parents != (original.id, *ids)
    ):
        raise ValueError("Candidate history has a malformed committed closure")
    index = index_published_records(records)
    members = []
    for record_id in ids:
        matches = index.by_id.get(record_id, ())
        if len(matches) != 1:
            raise ValueError("Candidate committed member is missing or ambiguous")
        member = matches[0]
        validate_runtime_record(member)
        if (
            head.payload.get("closure_version") == 1
            and head.id in member.causal_parents
        ):
            raise ValueError("Candidate closure contains a causal cycle")
        members.append(member)
    validate_runtime_record(original)
    validate_runtime_record(head)
    return CommitmentDecision(
        candidate_id=candidate_id,
        accepted=True,
        runtime_records=(original, *members, head),
        trace_entries=tuple(
            trace_entry_from_dict(row.payload["trace"])
            for row in members
            if row.record_type == "trace.recorded"
        ),
        contracts=tuple(
            contract_from_dict(row.payload["contract"])
            for row in members
            if row.record_type == "contract.declared"
        ),
        assets=tuple(
            asset_from_dict(row.payload["asset"])
            for row in members
            if row.record_type == "asset.committed"
        ),
    )


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
    if allow_additional_causal_parents and "candidate_id" in member.payload:
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
