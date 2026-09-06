"""Read-only productivity projections for one task lineage.

The runtime Store remains the source of truth.  This module only walks the
immutable Contract and RuntimeRecord views and returns a deterministic JSON
shape for operator-facing productivity reporting.  In particular, lineage
selection lives here so CLI views do not grow competing descendant walkers.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from aigineering.core.runtime_projection import TERMINAL_EVENTS
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract, TraceEntry
from aigineering.protocol.wire import trace_entry_from_dict

_TOOL_METHODS = frozenset({"tool", "parallel_tool_item"})
_FAILURE_TERMINALS = frozenset({"failed", "cancelled", "unreachable", "stalled"})
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def project_task_productivity(contract: Contract, store) -> dict[str, Any]:
    """Project productivity facts for *contract* and all immutable descendants.

    The function is intentionally read-only.  It does not call completion or
    recovery code, mutate materializations, or infer facts from process state.
    Every count is derived from Contracts, Assets, durable RuntimeRecords, and
    durable ``trace.recorded`` entries currently visible in *store*.
    """

    contracts = _lineage_contracts(contract, store)
    contract_ids = {item.id for item in contracts}
    records = tuple(
        record
        for _revision, record in sorted(
            store.scan_runtime_records(), key=lambda item: item[0]
        )
    )
    traces = _trace_entries(records, contract_ids)
    terminal_by_contract = _terminal_states(records, traces, contract_ids)

    contract_states: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    tool_rows: list[dict[str, Any]] = []
    continuation_count = 0
    recovery_count = 0
    for item in contracts:
        terminal = terminal_by_contract.get(item.id)
        status = terminal or "active"
        status_counts[status] += 1
        if item.origin == "continuation":
            continuation_count += 1
        if item.origin == "recovery":
            recovery_count += 1
        row = {
            "contract_id": item.id,
            "parent_id": item.parent_id,
            "name": item.name,
            "origin": item.origin,
            "status": status,
            "terminal": terminal,
        }
        contract_states.append(row)

        method = _method(item)
        if _is_tool_contract(item, method):
            tool_rows.append(_tool_call_row(item, status, store))

    usage_records, token_totals = _usage_projection(traces, contracts)
    rejection_records, rejection_trace_count = _rejections(
        records, traces, contract_ids
    )
    rejection_count = max(len(rejection_records), rejection_trace_count)
    terminal_counts = Counter(
        terminal
        for terminal in terminal_by_contract.values()
        if terminal in TERMINAL_EVENTS
    )
    tool_counts = Counter(row["status"] for row in tool_rows)
    tool_summary = {
        "total": len(tool_rows),
        "succeeded": tool_counts.get("complete", 0),
        "failed": sum(tool_counts.get(value, 0) for value in _FAILURE_TERMINALS),
        "pending": tool_counts.get("active", 0),
        "calls": tool_rows,
    }
    continuation_scheduled = sum(
        1 for trace in traces if trace.event_type == "method_continuation_scheduled"
    )

    return {
        "root_contract_id": contract.id,
        "contract_count": len(contracts),
        "contract_ids": [item.id for item in contracts],
        "contracts": contract_states,
        "contract_statuses": dict(sorted(status_counts.items())),
        "terminal_count": len(terminal_by_contract),
        "terminal_statuses": dict(sorted(terminal_counts.items())),
        "tool_calls": tool_summary,
        "continuation_count": continuation_count,
        "continuation_scheduled_count": continuation_scheduled,
        "recovery_count": recovery_count,
        "rejection_count": rejection_count,
        "rejection_record_count": len(rejection_records),
        "rejection_trace_count": rejection_trace_count,
        "rejections": rejection_records,
        "usage_records": usage_records,
        "token_totals": token_totals,
    }


def _lineage_contracts(root: Contract, store) -> list[Contract]:
    """Return root plus descendants using one deterministic graph walk."""

    contracts_by_id = {item.id: item for item in store.get_all_contracts()}
    contracts_by_id[root.id] = root
    children: defaultdict[str, list[Contract]] = defaultdict(list)
    for item in contracts_by_id.values():
        if item.parent_id is not None:
            children[item.parent_id].append(item)

    result: list[Contract] = []
    visited: set[str] = set()
    pending = [root.id]
    while pending:
        current_id = pending.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        current = contracts_by_id.get(current_id)
        if current is None:
            continue
        result.append(current)
        pending.extend(
            child.id
            for child in sorted(
                children.get(current_id, ()), key=lambda value: value.id, reverse=True
            )
        )
    return result


def _trace_entries(records: Iterable[Any], contract_ids: set[str]) -> list[TraceEntry]:
    traces: list[TraceEntry] = []
    seen: set[str] = set()
    for record in records:
        if record.record_type != "trace.recorded":
            continue
        payload = record.payload.get("trace")
        if not isinstance(payload, Mapping):
            continue
        try:
            trace = trace_entry_from_dict(payload)
        except (TypeError, ValueError, KeyError):
            continue
        if trace.id in seen or trace.contract_id not in contract_ids:
            continue
        seen.add(trace.id)
        traces.append(trace)
    return traces


def _terminal_states(
    records: Iterable[Any], traces: Iterable[TraceEntry], contract_ids: set[str]
) -> dict[str, str]:
    states: dict[str, str] = {}
    for trace in traces:
        if trace.event_type in TERMINAL_EVENTS:
            states[trace.contract_id] = trace.event_type
    for record in records:
        if record.record_type != "lifecycle.terminal":
            continue
        contract_id = str(record.payload.get("contract_id", ""))
        terminal = str(record.payload.get("terminal", ""))
        if contract_id in contract_ids and terminal in TERMINAL_EVENTS:
            states[contract_id] = terminal
    return states


def _method(contract: Contract) -> str:
    value = _method_payload(contract).get("method")
    return value if isinstance(value, str) else ""


def _method_payload(contract: Contract) -> Mapping[str, Any]:
    """Read method metadata without coupling the core projection to plugins."""

    if not contract.description:
        return {}
    try:
        parsed = json.loads(contract.description)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _is_tool_contract(contract: Contract, method: str) -> bool:
    if method in _TOOL_METHODS:
        return True
    return any(
        label in {"plugin:tool", "plugin:parallel_tool_item"}
        for label in contract.labels
    )


def _tool_call_row(contract: Contract, status: str, store) -> dict[str, Any]:
    method = _method(contract)
    call_name = ""
    payload = _method_payload(contract).get("payload")
    if isinstance(payload, Mapping) and isinstance(payload.get("name"), str):
        call_name = str(payload["name"])
    observed: bool | None = None
    try:
        assets = store.get_assets_by_contract(contract.id)
    except AttributeError:
        assets = []
    for asset in assets:
        if asset.name not in contract.outputs:
            continue
        try:
            parsed = json.loads(asset.content)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, Mapping) and isinstance(parsed.get("ok"), bool):
            observed = bool(parsed["ok"])
            if observed is False:
                break
    effective_status = status
    if observed is False:
        effective_status = "failed"
    elif observed is True and status == "active":
        effective_status = "complete"
    return {
        "contract_id": contract.id,
        "parent_id": contract.parent_id,
        "method": method,
        "tool": call_name,
        "status": effective_status,
        "observation_ok": observed,
    }


def _usage_projection(
    traces: Iterable[TraceEntry], contracts: list[Contract]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    methods = {item.id: _method(item) for item in contracts}
    usage_records: list[dict[str, Any]] = []
    totals = {field: 0 for field in _TOKEN_FIELDS}
    for trace in traces:
        metadata = trace.usage_metadata
        if metadata is None:
            continue
        usage = deep_thaw(metadata)
        if not isinstance(usage, Mapping):
            continue
        usage_dict = dict(usage)
        kind = _usage_kind(trace, methods.get(trace.contract_id, ""), usage_dict)
        usage_records.append(
            {
                "contract_id": trace.contract_id,
                "event_type": trace.event_type,
                "worker_id": trace.worker_id,
                "kind": kind,
                "usage": usage_dict,
            }
        )
        for field in _TOKEN_FIELDS:
            value = usage_dict.get(field)
            if type(value) is int and value >= 0:
                totals[field] += value
        if "total_tokens" not in usage_dict:
            prompt = usage_dict.get("prompt_tokens")
            completion = usage_dict.get("completion_tokens")
            if (
                type(prompt) is int
                and prompt >= 0
                and type(completion) is int
                and completion >= 0
            ):
                totals["total_tokens"] += prompt + completion
    return usage_records, totals


def _usage_kind(trace: TraceEntry, method: str, usage: Mapping[str, Any]) -> str:
    if method in _TOOL_METHODS:
        return "tool"
    worker_id = str(trace.worker_id or "")
    if worker_id.startswith(("tool_worker", "mcp_worker")):
        return "tool"
    if any(
        key in usage
        for key in (
            "model",
            "provider",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        )
    ):
        return "llm"
    return "worker"


def _rejections(
    records: Iterable[Any], traces: Iterable[TraceEntry], contract_ids: set[str]
) -> tuple[list[dict[str, Any]], int]:
    values: list[dict[str, Any]] = []
    for record in records:
        payload = record.payload
        contract_id = str(payload.get("contract_id", ""))
        rejected = "rejected" in record.record_type or (
            record.record_type == "projection.decided"
            and payload.get("status") == "rejected"
        )
        if not rejected or contract_id not in contract_ids:
            continue
        values.append(
            {
                "record_id": record.id,
                "record_type": record.record_type,
                "contract_id": contract_id,
                "reason": str(payload.get("reason", "")),
            }
        )
    trace_count = sum(
        1
        for trace in traces
        if trace.contract_id in contract_ids and trace.rejected_fragments
    )
    return values, trace_count
