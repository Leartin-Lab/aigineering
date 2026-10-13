"""Shared application task views derived from immutable runtime facts."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aigineering.core.runtime_projection import (
    TERMINAL_EVENTS,
    ContractView,
    RuntimeProjection,
)
from aigineering.core.claim_constraints import (
    claim_constraint_blockers,
    open_claim_constraints,
)
from aigineering.core.store import require_operational_store
from aigineering.core.acceptance import (
    validate_claim_bound_output,
    validate_output_shape,
)
from aigineering.core.contract_admission import validate_acceptance_policy
from aigineering.core.output_satisfaction import (
    _is_contract_or_descendant,
    all_outputs_satisfied,
    is_business_output,
)
from aigineering.core.worker_routing import eligible_workers
from aigineering.core.claim_assignments import (
    assignment_blocker,
    assignment_context_from_projection,
    assignment_head,
    effective_assignment,
)
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.plugins.task_priority import PRIORITY_FACT, priority_histories
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract, TraceEntry


def contract_tree_ids(contracts: list[Contract], root_contract_id: str) -> set[str]:
    """Select an immutable root and descendants for every application entry."""
    by_id = {contract.id: contract for contract in contracts}
    selected = {root_contract_id} if root_contract_id in by_id else set()
    changed = True
    while changed:
        changed = False
        for contract in contracts:
            if contract.parent_id in selected and contract.id not in selected:
                selected.add(contract.id)
                changed = True
    return selected


def task_priority_histories(projection_context, contract_ids) -> dict[str, tuple]:
    """Read strict priority histories, defaulting only fact-free tasks to zero.

    This is a presentation compatibility rule for legacy materialized
    Contracts. Once a priority fact exists, the plugin's complete provenance
    and declaration validation remains mandatory.
    """
    requested = tuple(contract_ids)
    if any(not isinstance(item, str) or not item for item in requested):
        raise ValueError("contract_ids must contain non-empty IDs")
    if len(requested) != len(set(requested)):
        raise ValueError("contract_ids must not contain duplicates")
    fact_contract_ids = {
        record.payload.get("contract_id")
        for record in projection_context.runtime_records
        if record.record_type == PRIORITY_FACT
    }
    strict_ids = tuple(item for item in requested if item in fact_contract_ids)
    histories = priority_histories(projection_context, strict_ids)
    return {contract_id: histories.get(contract_id, ()) for contract_id in requested}


def project_task_status(
    contract: Contract,
    store,
    *,
    check_integrity: bool = False,
    projection_context=None,
    priority_facts=None,
    query_context=None,
) -> dict:
    """Return a JSON-serializable task status projection.

    This is a read-only view over durable contracts, assets, and trace records.
    It is intentionally not runtime truth; it is the agent-facing projection
    used by ``aig task status``, ``wait``, and ``audit``.
    """
    store = require_operational_store(store)
    original_store = store
    if query_context is not None:
        query_context.require_match(store, projection_context=projection_context)
        if query_context.read_store.get_contract(contract.id) != contract:
            raise ValueError("task-view Contract does not belong to its query snapshot")
        store = query_context.read_store
    entries = _trace_entries(store, contract.id)
    view = (
        query_context.runtime_projection.contract_view(contract)
        if query_context is not None
        else RuntimeProjection(store, store).contract_view(contract)
    )
    runtime_records = tuple(
        record for _revision, record in store.scan_runtime_records()
    )
    open_dependencies = open_claim_constraints(runtime_records, contract_id=contract.id)
    dependency_blockers = claim_constraint_blockers(
        runtime_records, contract_id=contract.id
    )
    terminal = _latest_terminal(entries) or _runtime_terminal(store, contract.id)
    attempt_outcome = _latest_attempt_outcome(store, contract.id)
    output_assets = _output_assets(contract, store)
    projection_context = projection_context or (
        query_context.effect_projection
        if query_context is not None
        else load_effect_projection_context(store)
    )
    candidate_qualification_outputs = (
        _submitted_independent_output_assets(contract, store, projection_context)
        if view.claim_status == "submitted"
        else {}
    )
    assignment_context = assignment_context_from_projection(projection_context)
    assignment_fact = assignment_head(assignment_context, contract_id=contract.id)
    effective_worker = effective_assignment(assignment_context, contract_id=contract.id)
    assignment_issue = assignment_blocker(assignment_context, contract_id=contract.id)
    if priority_facts is None:
        priority_facts = task_priority_histories(projection_context, (contract.id,))[
            contract.id
        ]
    priority_fact = priority_facts[-1] if priority_facts else None
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
        contract,
        store,
        entries,
        terminal,
        view,
        attempt_outcome,
        assignment_blocker=assignment_issue,
    )
    qualification_pending_outputs = (
        candidate_qualification_outputs if status == "submitted" else {}
    )
    qualification_pending = bool(qualification_pending_outputs)
    failure_details = _descendant_failure_details(
        contract,
        store,
        include_contract=status in {"failed", "cancelled", "unreachable", "stalled"},
    )
    blockers = list(view.blockers)
    if qualification_pending and "qualification:independent_acceptance" not in blockers:
        blockers.append("qualification:independent_acceptance")
    if assignment_issue is not None:
        marker = f"assignment:{assignment_issue['assignment_id']}:{assignment_issue['reason']}"
        if marker not in blockers:
            blockers.append(marker)
    if status not in {"completed", "failed", "cancelled", "unreachable"}:
        blockers.extend(
            f"descendant_{item['status']}:{item['contract_id']}"
            for item in failure_details
        )
    risks = _silent_failure_risks(
        contract,
        store,
        entries,
        status,
        view=view,
        qualification_pending=qualification_pending,
    )
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
        "dependency_blockers": list(dependency_blockers),
        "dependencies": [
            {
                "constraint_id": record.id,
                "prerequisite_contract_id": str(
                    record.payload["prerequisite_contract_id"]
                ),
                "predicate": str(record.payload["predicate"]),
                "required_terminal": str(record.payload["required_terminal"]),
                "reason": str(record.payload["reason"]),
                "active": True,
            }
            for record in open_dependencies
        ],
        "budget_remaining": view.budget_remaining,
        "projection_hash": view.projection_hash,
        "outputs": output_assets,
        "qualification_pending_outputs": qualification_pending_outputs,
        "silent_failure_risks": risks,
        "rejection_count": len(rejections),
        "rejections": rejections,
        "recovery_count": len(recoveries),
        "recoveries": recoveries,
        "trace_event_count": len(entries),
        "token_usage": usage,
        "failure_details": failure_details,
        "assignment": (
            {
                "assignment_id": assignment_fact.id,
                "worker_id": (
                    str(effective_worker.payload["worker_id"])
                    if effective_worker is not None
                    else None
                ),
                "blocker": dict(assignment_issue) if assignment_issue else None,
            }
            if assignment_fact is not None
            else None
        ),
        "priority": (
            int(priority_fact.payload["priority"]) if priority_fact is not None else 0
        ),
        "priority_fact_id": priority_fact.id if priority_fact is not None else None,
    }
    if check_integrity:
        from aigineering.core.integrity import check_store_integrity

        result["integrity"] = check_store_integrity(original_store)
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
    *,
    assignment_blocker: dict | None = None,
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
    if view.claim_status == "active":
        return "claimed"
    if view.claim_status == "submitted":
        return "submitted"
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
    if any(
        blocker.startswith(("claim_hold:", "prerequisite:"))
        for blocker in view.blockers
    ):
        return "blocked"
    if not view.inputs_satisfied or not view.activation_satisfied:
        return "blocked"
    if view.budget_remaining <= 0:
        return "stalled"
    if assignment_blocker is not None:
        return "blocked_assignment"
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
    qualification_pending: bool = False,
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
    if status == "submitted" and not active_recovery and not qualification_pending:
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


def _submitted_independent_output_assets(
    contract: Contract, store, projection_context=None
) -> dict[str, str]:
    """Return exact eligible output Assets awaiting independent qualification."""
    policy = contract.acceptance_policy
    if policy is None or policy.get("mode") != "independent" or not contract.outputs:
        return {}
    try:
        validate_acceptance_policy(contract)
    except (TypeError, ValueError):
        return {}
    projection_context = projection_context or load_effect_projection_context(store)

    committed_asset_ids = {
        str(asset_payload.get("id", ""))
        for _revision, record in store.scan_runtime_records(
            record_type="asset.committed"
        )
        if isinstance((asset_payload := record.payload.get("asset")), Mapping)
    }
    for field in ("rubric_asset_ids", "evidence_asset_ids"):
        for asset_id in policy.get(field, ()):
            asset = store.get_asset(asset_id)
            if (
                asset is None
                or asset_id not in committed_asset_ids
                or asset.tombstoned
                or not asset.promptable
                or asset.disclosure_view != "original"
            ):
                return {}

    output_assets: dict[str, str] = {}
    for output_name in contract.outputs:
        qualified_asset_ids = {
            str(record.payload.get("asset_id", ""))
            for _revision, record in store.scan_runtime_records(
                record_type="output.qualified"
            )
            if record.payload.get("contract_id") == contract.id
            and record.payload.get("output_name") == output_name
        }
        matching_assets = store.get_assets_by_name(output_name)
        if any(
            asset.id in qualified_asset_ids
            and is_business_output(asset, output_name)
            and _is_contract_or_descendant(asset.created_by, contract.id, store)
            for asset in matching_assets
        ):
            continue

        eligible_asset_id = None
        for asset in matching_assets:
            try:
                validate_claim_bound_output(
                    projection_context,
                    asset,
                    next(
                        item
                        for item in projection_context.contracts
                        if item.id == asset.created_by
                    ),
                )
            except (LookupError, StopIteration, ValueError):
                continue
            if (
                asset.tombstoned
                or not asset.promptable
                or asset.disclosure_view != "original"
                or not is_business_output(asset, output_name)
                or not _is_contract_or_descendant(asset.created_by, contract.id, store)
                or asset.id not in committed_asset_ids
            ):
                continue
            try:
                validate_output_shape(policy, output_name, asset.content)
            except (TypeError, ValueError):
                continue
            eligible_asset_id = asset.id
            break
        if eligible_asset_id is None:
            # An unqualified slot without an eligible business assertion is
            # not part of a complete submitted output set.
            return {}
        output_assets[output_name] = eligible_asset_id
    return (
        output_assets
        if output_assets and not all_outputs_satisfied(contract, store)
        else {}
    )


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
                qualification_pending=(
                    view.claim_status == "submitted"
                    and bool(_submitted_independent_output_assets(child, store))
                ),
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
