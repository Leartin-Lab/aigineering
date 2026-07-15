"""Validation for immutable, single-assignment Contract terminal facts."""

from __future__ import annotations

from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.runtime_projection import TERMINAL_EVENTS
from aigineering.protocol.runtime_record import RuntimeRecord


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
