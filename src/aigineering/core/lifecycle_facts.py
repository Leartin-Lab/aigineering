"""Validation for immutable, single-assignment Contract terminal facts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from aigineering.core.causal_allowance import resolve_causal_allowance
from aigineering.core.store_capabilities import CandidateCommitStore, TraceReader
from aigineering.core.trace import create_entry, trace_record_from_entry
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.runtime_projection import TERMINAL_EVENTS
from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract


class TerminalCommitStore(CandidateCommitStore, TraceReader, Protocol):
    """Capabilities required by the canonical derived-terminal commit."""


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


def commit_terminal_outcome(
    store: TerminalCommitStore,
    contract: Contract,
    event_type: str,
    *,
    relation_type: str = "",
    relation_target: str = "",
    reason: str = "",
) -> bool:
    """Atomically commit one derived terminal fact and its audit trace."""
    existing_trace = any(
        entry.event_type in TERMINAL_EVENTS and entry.contract_id == contract.id
        for entry in store.get_all()
    )
    existing_fact = any(
        str(record.payload.get("contract_id", "")) == contract.id
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    )
    if existing_trace or existing_fact:
        return False
    trace_kwargs = {
        "budget_remaining": resolve_causal_allowance(
            store, contract, fallback=contract.budget
        )
    }
    if relation_type:
        trace_kwargs.update(
            relation_type=relation_type,
            relation_target=relation_target or contract.id,
            rejected_fragments=[f"[{event_type}] {relation_type}: {reason}"],
        )
    entry = create_entry(contract.id, event_type, **trace_kwargs)
    terminal = create_terminal_record(contract.id, event_type)
    store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=[entry],
        runtime_records=(terminal, trace_record_from_entry(entry)),
    )
    return True
