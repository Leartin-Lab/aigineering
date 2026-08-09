"""Task status projections for agent-facing CLI commands."""

from __future__ import annotations

from aigineering.core.runtime_projection import (
    TERMINAL_EVENTS,
    ContractView,
    RuntimeProjection,
)
from aigineering.core.store import require_operational_store
from aigineering.core.output_satisfaction import all_outputs_satisfied
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
    terminal = _latest_terminal(entries) or _runtime_terminal(store, contract.id)
    attempt_outcome = _latest_attempt_outcome(store, contract.id)
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

    status = _status_from_entries(
        contract, store, entries, terminal, view, attempt_outcome
    )
    risks = _silent_failure_risks(contract, store, entries, status, view=view)
    if status not in {"completed", "failed", "cancelled", "unreachable"}:
        risks.extend(_descendant_failure_risks(contract, store))
    return {
        "contract_id": contract.id,
        "name": contract.name,
        "status": status,
        "terminal": status in TERMINAL_EVENTS or status == "completed",
        "ok": status == "completed",
        "outputs_satisfied": all_outputs_satisfied(contract, store),
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


def _runtime_terminal(store, contract_id: str) -> str:
    terminals = {
        str(record.payload["terminal"])
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == contract_id
    }
    if len(terminals) > 1:
        return "stalled"
    return next(iter(terminals), "")


def _latest_attempt_outcome(store, contract_id: str) -> str:
    outcomes = [
        str(record.payload.get("outcome", ""))
        for _, record in store.scan_runtime_records(record_type="attempt.closed")
        if record.payload.get("contract_id") == contract_id
    ]
    return outcomes[-1] if outcomes else ""


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
    attempt_outcome: str,
) -> str:
    if view.terminal == "conflict":
        return "stalled"
    if view.terminal is not None:
        return "completed" if view.terminal == "complete" else view.terminal
    if terminal == "complete":
        return "completed"
    if terminal:
        return terminal
    if all_outputs_satisfied(contract, store):
        return "completed"
    if attempt_outcome == "expanded":
        return "expanded"
    if any(entry.event_type == "projection" for entry in entries):
        return "submitted"
    suspended = False
    for entry in entries:
        if entry.event_type in {
            "task_delegated",
            "method_scheduled",
            "method_continuation_scheduled",
        }:
            suspended = True
        elif entry.event_type == "method_resumed":
            suspended = False
    if suspended:
        return "blocked_delegation"
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
    *,
    has_active_recovery: bool | None = None,
    view: ContractView | None = None,
) -> list[dict[str, str]]:
    if status in {"completed", "failed", "cancelled", "unreachable", "stalled"}:
        return []
    risks: list[dict[str, str]] = []
    effective_view = view or RuntimeProjection(store, store).contract_view(contract)
    if "invalid_activation" in effective_view.blockers:
        risks.append(
            {
                "code": "invalid_activation",
                "message": "task activation expression is not valid executable syntax",
            }
        )
    if (
        not all_outputs_satisfied(contract, store)
        and effective_view.budget_remaining <= 0
    ):
        risks.append(
            {
                "code": "budget_exhausted",
                "message": "budget is exhausted before declared outputs are satisfied",
            }
        )
    active_recovery = (
        _has_active_recovery(contract, store)
        if has_active_recovery is None
        else has_active_recovery
    )
    if status == "submitted" and not active_recovery:
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


def _has_active_recovery(contract: Contract, store) -> bool:
    for candidate in store.get_all_contracts():
        if candidate.origin != "recovery":
            continue
        if candidate.parent_id == contract.id or candidate.name.startswith(
            f"{contract.name}.recover"
        ):
            return True
    return False


def _descendant_failure_risks(contract: Contract, store) -> list[dict[str, str]]:
    contracts = store.get_all_contracts()
    children: dict[str, list[Contract]] = {}
    contracts_by_name = {candidate.name: candidate.id for candidate in contracts}
    active_recovery_for: set[str] = set()
    for candidate in contracts:
        if candidate.parent_id is not None:
            children.setdefault(candidate.parent_id, []).append(candidate)
        if candidate.origin == "recovery":
            if candidate.parent_id is not None:
                active_recovery_for.add(candidate.parent_id)
            base_name = candidate.name.split(".recover", 1)[0]
            parent_id = contracts_by_name.get(base_name)
            if parent_id is not None:
                active_recovery_for.add(parent_id)

    risks: list[dict[str, str]] = []
    visited: set[str] = {contract.id}
    projection = RuntimeProjection(store, store)
    pending = [contract.id]
    while pending:
        parent_id = pending.pop()
        for child in children.get(parent_id, []):
            if child.id in visited:
                continue
            visited.add(child.id)
            entries = _trace_entries(store, child.id)
            view = projection.contract_view(child)
            terminal = _latest_terminal(entries)
            status = _status_from_entries(
                child,
                store,
                entries,
                terminal,
                view,
                _latest_attempt_outcome(store, child.id),
            )
            if status in {"failed", "cancelled", "unreachable", "stalled"}:
                risks.append(
                    {
                        "code": f"descendant_{status}",
                        "message": (
                            f"descendant task {child.id} ({child.name}) is {status}"
                        ),
                    }
                )
            child_risks = _silent_failure_risks(
                child,
                store,
                entries,
                status,
                has_active_recovery=child.id in active_recovery_for,
                view=view,
            )
            for risk in child_risks:
                risks.append(
                    {
                        "code": f"descendant_{risk['code']}",
                        "message": (
                            f"descendant task {child.id} ({child.name}): "
                            f"{risk['message']}"
                        ),
                    }
                )
            pending.append(child.id)
    return risks
