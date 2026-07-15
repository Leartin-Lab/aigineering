"""Task status projections for agent-facing CLI commands."""

from __future__ import annotations

from aigineering.core.runtime_projection import (
    TERMINAL_EVENTS,
    ContractView,
    RuntimeProjection,
)
from aigineering.core.store import require_operational_store
from aigineering.core.submit import _all_outputs_satisfied
from aigineering.core.worker_routing import eligible_workers
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract, TraceEntry


def project_task_status(contract: Contract, store) -> dict:
    """Return a JSON-serializable task status projection.

    This is a read-only view over durable contracts, assets, and trace records.
    It is intentionally not runtime truth; it is the agent-facing projection
    used by ``aig task status``, ``wait``, and ``audit``.
    """
    store = require_operational_store(store)
    entries = _trace_entries(store, contract.id)
    view = RuntimeProjection(store, store).contract_view(contract)
    terminal = _latest_terminal(entries)
    output_assets = _output_assets(contract, store)
    rejections = [
        fragment
        for entry in entries
        for fragment in tuple(entry.rejected_fragments or ())
    ]
    recoveries = [
        c.id
        for c in store.get_all_contracts()
        if c.origin == "recovery"
        and (
            c.parent_id == contract.id or c.name.startswith(f"{contract.name}.recover")
        )
    ]
    usage = [
        deep_thaw(entry.usage_metadata)
        for entry in entries
        if entry.usage_metadata is not None
    ]

    status = _status_from_entries(contract, store, entries, terminal, view)
    risks = _silent_failure_risks(contract, store, entries, status)
    return {
        "contract_id": contract.id,
        "name": contract.name,
        "status": status,
        "terminal": status in TERMINAL_EVENTS or status == "completed",
        "ok": status == "completed",
        "outputs_satisfied": _all_outputs_satisfied(contract, store),
        "blockers": list(view.blockers),
        "budget_remaining": view.budget_remaining,
        "projection_hash": view.projection_hash,
        "outputs": output_assets,
        "silent_failure_risks": risks,
        "rejection_count": len(rejections),
        "rejections": rejections,
        "recovery_count": len(recoveries),
        "recoveries": recoveries,
        "trace_event_count": len(entries),
        "token_usage": usage,
    }


def _trace_entries(store, contract_id: str) -> list[TraceEntry]:
    return list(store.get_by_contract(contract_id))


def _latest_terminal(entries: list[TraceEntry]) -> str:
    terminal = ""
    for entry in entries:
        if entry.event_type in TERMINAL_EVENTS:
            terminal = entry.event_type
    return terminal


def _output_assets(contract: Contract, store) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for name in contract.outputs:
        matches = store.get_assets_by_name(name)
        if matches:
            outputs[name] = matches[-1].id
    return outputs


def _status_from_entries(
    contract: Contract,
    store,
    entries: list[TraceEntry],
    terminal: str,
    view: ContractView,
) -> str:
    if view.terminal == "conflict":
        return "stalled"
    if terminal == "complete":
        return "completed"
    if terminal:
        return terminal
    if _all_outputs_satisfied(contract, store):
        return "completed"
    if any(entry.event_type == "projection" for entry in entries):
        return "submitted"
    suspended = False
    for entry in entries:
        if entry.event_type in {"method_scheduled", "method_continuation_scheduled"}:
            suspended = True
        elif entry.event_type == "method_resumed":
            suspended = False
    if suspended:
        return "blocked_method"
    if view.claim_status == "active":
        return "claimed"
    if not view.inputs_satisfied or not view.activation_satisfied:
        return "blocked"
    if view.budget_remaining <= 0:
        return "stalled"
    if contract.worker_capabilities or contract.worker_pools:
        if not eligible_workers(contract, store.get_worker_registrations()):
            return "blocked_capability"
    return "ready"


def _silent_failure_risks(
    contract: Contract,
    store,
    entries: list[TraceEntry],
    status: str,
) -> list[dict[str, str]]:
    if status in {"completed", "failed", "cancelled", "unreachable"}:
        return []
    risks: list[dict[str, str]] = []
    if (
        not _all_outputs_satisfied(contract, store)
        and _budget_remaining(contract, entries) <= 0
    ):
        risks.append(
            {
                "code": "budget_exhausted",
                "message": "budget is exhausted before declared outputs are satisfied",
            }
        )
    if status == "submitted" and not _has_active_recovery(contract, store):
        risks.append(
            {
                "code": "submitted_without_recovery",
                "message": "task has worker projection but no terminal event or active recovery",
            }
        )
    if status == "blocked_capability":
        risks.append(
            {
                "code": "blocked_capability",
                "message": "no registered worker currently satisfies routing constraints",
            }
        )
    return risks


def _budget_remaining(contract: Contract, entries: list[TraceEntry]) -> int:
    remaining = max(contract.budget, 1)
    for entry in entries:
        if entry.event_type in {"budget_initialized", "budget_consumed"}:
            remaining = entry.budget_remaining
    return remaining


def _has_active_recovery(contract: Contract, store) -> bool:
    for candidate in store.get_all_contracts():
        if candidate.origin != "recovery":
            continue
        if candidate.parent_id == contract.id or candidate.name.startswith(
            f"{contract.name}.recover"
        ):
            return True
    return False
