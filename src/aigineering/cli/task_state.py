"""Task status projections for agent-facing CLI commands."""

from __future__ import annotations

import json

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


def project_task_status(
    contract: Contract, store, *, check_integrity: bool = False
) -> dict:
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
    recoveries = recovery_successor_ids(contract, store)
    usage = [
        deep_thaw(entry.usage_metadata)
        for entry in entries
        if entry.usage_metadata is not None
    ]

    status = _status_from_entries(
        contract, store, entries, terminal, view, attempt_outcome
    )
    failure_details = _descendant_failure_details(
        contract,
        store,
        include_contract=status in {"failed", "cancelled", "unreachable", "stalled"},
    )
    blockers = list(view.blockers)
    if status not in {"completed", "failed", "cancelled", "unreachable"}:
        blockers.extend(
            f"descendant_{item['status']}:{item['contract_id']}"
            for item in failure_details
        )
    risks = _silent_failure_risks(contract, store, entries, status, view=view)
    if status not in {"completed", "failed", "cancelled", "unreachable"}:
        risks.extend(_descendant_failure_risks(contract, store))
    result = {
        "contract_id": contract.id,
        "name": contract.name,
        "status": status,
        "terminal": status in TERMINAL_EVENTS or status == "completed",
        "ok": status == "completed",
        "outputs_satisfied": all_outputs_satisfied(contract, store),
        "blockers": blockers,
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
        "failure_details": failure_details,
    }
    if check_integrity:
        from aigineering.core.integrity import check_store_integrity

        result["integrity"] = check_store_integrity(store)
        result["ok"] = result["ok"] and result["integrity"]["consistent"]
    return result


def _trace_entries(store, contract_id: str) -> list[TraceEntry]:
    return list(store.get_by_contract(contract_id))


def recovery_successor_ids(
    contract: Contract, store, *, contracts: list[Contract] | None = None
) -> list[str]:
    """Resolve recovery successors through durable identity relations only."""
    return recovery_successor_map(
        store, contracts, source_contract_ids={contract.id}
    ).get(contract.id, [])


def recovery_successor_map(
    store,
    contracts: list[Contract] | None = None,
    *,
    source_contract_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Build exact failed-task to recovery relations from durable identities."""
    contracts = contracts if contracts is not None else store.get_all_contracts()
    recoveries = [item for item in contracts if item.origin == "recovery"]
    if not recoveries:
        return {}
    contract_ids = {item.id for item in contracts}
    recovery_ids = {item.id for item in recoveries}
    successors: dict[str, set[str]] = {}
    for item in recoveries:
        if item.parent_id in contract_ids:
            successors.setdefault(item.parent_id, set()).add(item.id)
        context_ids = {
            name.removeprefix("_fail_context_")
            for name in item.inputs
            if name.startswith("_fail_context_")
        }
        try:
            description = json.loads(item.description)
        except (RecursionError, TypeError, json.JSONDecodeError):
            description = None
        reference_ids = set(context_ids)
        if isinstance(description, dict):
            described_id = description.get("failed_contract_id")
            if isinstance(described_id, str):
                reference_ids.add(described_id)
        if len(reference_ids) == 1:
            failed_id = next(iter(reference_ids))
            if failed_id in contract_ids:
                successors.setdefault(failed_id, set()).add(item.id)

    related_events = {"recovery_scheduled", "method_recovery_scheduled", "failed"}
    for contract in contracts:
        if source_contract_ids is not None and contract.id not in source_contract_ids:
            continue
        for entry in _trace_entries(store, contract.id):
            if (
                entry.event_type in related_events
                and entry.relation_target in recovery_ids
            ):
                successors.setdefault(contract.id, set()).add(entry.relation_target)
    return {
        contract_id: sorted(recovery_ids)
        for contract_id, recovery_ids in successors.items()
    }


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
        and not _has_unfinished_descendant(contract, store)
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


def _has_unfinished_descendant(contract: Contract, store) -> bool:
    children: dict[str, list[str]] = {}
    for candidate in store.get_all_contracts():
        if candidate.parent_id is not None:
            children.setdefault(candidate.parent_id, []).append(candidate.id)
    terminal_ids = {
        str(record.payload.get("contract_id", ""))
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    }
    pending = list(children.get(contract.id, ()))
    visited: set[str] = set()
    while pending:
        contract_id = pending.pop()
        if contract_id in visited:
            continue
        visited.add(contract_id)
        if contract_id not in terminal_ids:
            return True
        pending.extend(children.get(contract_id, ()))
    return False


def _has_active_recovery(contract: Contract, store) -> bool:
    return bool(recovery_successor_ids(contract, store))


def _descendant_failure_risks(contract: Contract, store) -> list[dict[str, str]]:
    contracts = store.get_all_contracts()
    children: dict[str, list[Contract]] = {}
    recovery_map = recovery_successor_map(store, contracts)
    for candidate in contracts:
        if candidate.parent_id is not None:
            children.setdefault(candidate.parent_id, []).append(candidate)
    active_recovery_for = set(recovery_map)

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


def _descendant_failure_details(
    contract: Contract, store, *, include_contract: bool = False
) -> list[dict[str, object]]:
    """Expose bounded failure facts and separately labeled related tool errors."""
    contracts = store.get_all_contracts()
    contracts_by_id = {candidate.id: candidate for candidate in contracts}
    children: dict[str, list[Contract]] = {}
    for candidate in contracts:
        if candidate.parent_id is not None:
            children.setdefault(candidate.parent_id, []).append(candidate)
    projection = RuntimeProjection(store, store)
    details: list[dict[str, object]] = []
    pending = ([contract] if include_contract else []) + list(
        children.get(contract.id, ())
    )
    visited: set[str] = set()
    while pending:
        child = pending.pop(0)
        if child.id in visited:
            continue
        visited.add(child.id)
        entries = _trace_entries(store, child.id)
        view = projection.contract_view(child)
        status = _status_from_entries(
            child,
            store,
            entries,
            _latest_terminal(entries),
            view,
            _latest_attempt_outcome(store, child.id),
        )
        if status in {"failed", "cancelled", "unreachable", "stalled"}:
            branch_root = child
            ancestor_ids = {branch_root.id}
            while branch_root.parent_id != contract.id:
                parent = contracts_by_id.get(branch_root.parent_id or "")
                if parent is None or parent.id in ancestor_ids:
                    break
                ancestor_ids.add(parent.id)
                branch_root = parent
            branch_contracts: list[Contract] = []
            branch_pending = [branch_root]
            branch_visited: set[str] = set()
            while branch_pending:
                branch_item = branch_pending.pop()
                if branch_item.id in branch_visited:
                    continue
                branch_visited.add(branch_item.id)
                branch_contracts.append(branch_item)
                branch_pending.extend(children.get(branch_item.id, ()))
            detail: dict[str, object] = {
                "contract_id": child.id,
                "name": child.name,
                "status": status,
                "tool": None,
                "error_type": None,
                "retryable": None,
                "observation_asset_ids": [],
                "cause_events": [],
                "related_tool_failures": [],
            }
            direct_entries = _trace_entries(store, child.id)
            for entry in direct_entries:
                metadata = (
                    deep_thaw(entry.usage_metadata)
                    if entry.usage_metadata is not None
                    else {}
                )
                if not metadata.get("error_type"):
                    continue
                cause = {
                    "source_contract_id": child.id,
                    "trace_id": entry.id,
                    "tool": metadata.get("tool"),
                    "error_type": metadata.get("error_type"),
                    "retryable": metadata.get("retryable"),
                    "observation_asset_ids": [],
                }
                cause["observation_asset_ids"] = _observation_asset_ids(entry, store)
                detail["cause_events"].append(cause)
                for key in ("tool", "error_type", "retryable"):
                    detail[key] = cause[key]
                detail["observation_asset_ids"].extend(
                    item
                    for item in cause["observation_asset_ids"]
                    if item not in detail["observation_asset_ids"]
                )

            for related_contract in branch_contracts:
                if related_contract.id == child.id:
                    continue
                for entry in _trace_entries(store, related_contract.id):
                    metadata = (
                        deep_thaw(entry.usage_metadata)
                        if entry.usage_metadata is not None
                        else {}
                    )
                    if not metadata.get("error_type"):
                        continue
                    detail["related_tool_failures"].append(
                        {
                            "source_contract_id": related_contract.id,
                            "trace_id": entry.id,
                            "tool": metadata.get("tool"),
                            "error_type": metadata.get("error_type"),
                            "retryable": metadata.get("retryable"),
                            "observation_asset_ids": _observation_asset_ids(
                                entry, store
                            ),
                        }
                    )
            details.append(detail)
        pending.extend(children.get(child.id, ()))
    return details


def _observation_asset_ids(entry: TraceEntry, store) -> list[str]:
    """Resolve observation references without interpreting effect descriptors."""
    ids: list[str] = []
    if entry.event_type in {"projection", "tool_executed", "fail_reported"}:
        for asset_id in entry.accepted_fragments or ():
            asset = store.get_asset(asset_id)
            if asset is not None and (
                asset.name.startswith("_tool_obs_")
                or asset.origin in {"tool_observation", "tool"}
            ):
                ids.append(asset.id)
    # Modern Candidate traces carry accepted names while immutable Asset facts
    # carry IDs. Resolve by name and source Contract, never by JSON fragments.
    for name in entry.accepted_asset_names or ():
        if not name.startswith(("_tool_obs_", "tool_observation_")):
            continue
        for asset in store.get_assets_by_name(name):
            is_observation = asset.name.startswith(
                ("_tool_obs_", "tool_observation_")
            ) or asset.origin in {"tool_observation", "tool"}
            if (
                is_observation
                and asset.created_by == entry.contract_id
                and asset.id not in ids
            ):
                ids.append(asset.id)
    return ids
