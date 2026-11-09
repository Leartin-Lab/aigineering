"""Signed Candidates for one-Contract Worker assignment transitions."""

from __future__ import annotations

from collections.abc import Iterable

from aigineering.protocol.runtime_record import RuntimeRecord

ASSIGNMENT_FACT = "claim.assignment.changed"
_FACT_FIELDS = frozenset(
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


def assignment_history(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return immutable assignment facts in the supplied durable order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type == ASSIGNMENT_FACT
        and (contract_id is None or record.payload.get("contract_id") == contract_id)
    )


__all__ = ["ASSIGNMENT_FACT", "assignment_history"]
