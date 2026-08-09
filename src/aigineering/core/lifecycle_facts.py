"""Validation for immutable, single-assignment Contract terminal facts."""

from __future__ import annotations

from collections.abc import Sequence

from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.runtime_projection import TERMINAL_EVENTS
from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.runtime_record import create_runtime_record


def create_terminal_record(
    contract_id: str,
    terminal: str,
    *,
    reason: str = "",
    actor_id: str = "",
    causal_parents: Sequence[str] = (),
    recorded_at: str | None = None,
) -> RuntimeRecord:
    """Construct one canonical terminal fact without assigning its cause."""
    if not contract_id or terminal not in TERMINAL_EVENTS:
        raise ValueError("terminal fact requires a Contract and valid terminal")
    payload = {"contract_id": contract_id, "terminal": terminal}
    if reason:
        payload["reason"] = reason
    if actor_id:
        payload["actor_id"] = actor_id
    return create_runtime_record(
        "lifecycle.terminal",
        payload,
        causal_parents=tuple(causal_parents),
        recorded_at=recorded_at,
    )


def validate_terminal_record(
    record: RuntimeRecord,
    existing: list[tuple[int, RuntimeRecord]],
) -> None:
    if record.record_type != "lifecycle.terminal":
        return
    contract_id = str(record.payload.get("contract_id", ""))
    terminal = str(record.payload.get("terminal", ""))
    if not contract_id or terminal not in TERMINAL_EVENTS:
        raise ValueError("lifecycle.terminal requires a contract and valid terminal")
    for _, current in existing:
        if str(current.payload.get("contract_id", "")) != contract_id:
            continue
        if current.id == record.id:
            return
        raise ImmutableRecordConflict("contract terminal", contract_id)
