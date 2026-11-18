"""Read-only validation of historical claim assignments records."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    validate_runtime_record,
)


ASSIGNMENT_FACT = "claim.assignment.changed"
_ASSIGNMENT_FIELDS = frozenset(
    {
        "actor_id",
        "candidate_id",
        "contract_id",
        "key_id",
        "previous_assignment_id",
        "reason",
        "worker_id",
    }
)
_MAX_TEXT_BYTES = 65536


class AssignmentConflict(ValueError):
    """An assignment fact or transition conflicts with current task state."""


@dataclass(frozen=True)
class AssignmentFactContext:
    projection: EffectProjectionContext


def assignment_context_from_projection(
    context: EffectProjectionContext,
) -> AssignmentFactContext:
    if not isinstance(context, EffectProjectionContext):
        raise TypeError("assignment context requires EffectProjectionContext")
    return AssignmentFactContext(context)


def _validate_assignment_record(record: RuntimeRecord) -> None:
    if record.record_type != ASSIGNMENT_FACT:
        return
    try:
        validate_runtime_record(record)
    except ValueError as exc:
        raise AssignmentConflict("assignment fact identity is invalid") from exc
    if record.schema_version != 1:
        raise AssignmentConflict("unsupported assignment fact schema version")
    payload = record.payload
    if set(payload) != _ASSIGNMENT_FIELDS:
        raise AssignmentConflict("assignment fact fields are invalid")
    for field in _ASSIGNMENT_FIELDS:
        value = payload[field]
        if not isinstance(value, str):
            raise AssignmentConflict(f"assignment {field} must be text")
        try:
            size = len(value.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise AssignmentConflict(f"assignment {field} is not valid UTF-8") from exc
        if size > _MAX_TEXT_BYTES:
            raise AssignmentConflict(f"assignment {field} is too long")
    for field in (
        "actor_id",
        "candidate_id",
        "contract_id",
        "key_id",
        "reason",
    ):
        if not payload[field].strip():
            raise AssignmentConflict(f"assignment {field} must not be blank")
    if not record.causal_parents:
        raise AssignmentConflict("assignment fact lacks Candidate provenance")


def assignment_history(
    context: AssignmentFactContext, *, contract_id: str
) -> tuple[RuntimeRecord, ...]:
    records = tuple(
        record
        for record in context.projection.runtime_records
        if record.record_type == ASSIGNMENT_FACT
        and record.payload.get("contract_id") == contract_id
    )
    heads: set[str] = set()
    previous = ""
    for record in records:
        _validate_assignment_record(record)
        if record.id in heads or record.payload["previous_assignment_id"] != previous:
            raise AssignmentConflict("assignment history predecessor is invalid")
        heads.add(record.id)
        previous = record.id
    return records


def assignment_head(
    context: AssignmentFactContext, *, contract_id: str
) -> RuntimeRecord | None:
    history = assignment_history(context, contract_id=contract_id)
    return history[-1] if history else None


def effective_assignment(
    context: AssignmentFactContext, *, contract_id: str
) -> RuntimeRecord | None:
    head = assignment_head(context, contract_id=contract_id)
    return head if head is not None and head.payload["worker_id"] else None
