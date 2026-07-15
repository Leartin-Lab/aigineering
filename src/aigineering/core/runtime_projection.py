"""Pure runtime views derived from immutable Contract, Asset, and Trace facts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from aigineering.core.activation import check_activation
from aigineering.core.ids import compute_content_hash
from aigineering.core.output_satisfaction import (
    all_outputs_satisfied,
    is_business_output,
)
from aigineering.protocol.wire import trace_entry_from_dict

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol
    from aigineering.protocol.types import Contract, TraceEntry


TERMINAL_EVENTS = frozenset({"complete", "failed", "cancelled", "unreachable"})
_ACTIVATION_OPERATORS = frozenset({"AND", "OR", "NOT"})


@dataclass(frozen=True)
class ContractView:
    """Deterministic execution view for one immutable Contract."""

    contract_id: str
    enabled: bool
    blockers: tuple[str, ...]
    activation_satisfied: bool
    inputs_satisfied: bool
    missing_assets: tuple[str, ...]
    outputs_satisfied: bool
    terminal: str | None
    terminal_events: tuple[str, ...]
    current_claim_id: str | None
    claim_status: str | None
    budget_remaining: int
    projection_hash: str


class RuntimeProjection:
    """Project runtime state without mutating Engine or persistence."""

    def __init__(
        self,
        store: StoreProtocol,
        trace: TraceStoreProtocol,
        *,
        as_of: str | None = None,
        as_of_revision: int | None = None,
    ) -> None:
        if as_of is not None and as_of_revision is not None:
            raise ValueError("choose either as_of timestamp or as_of_revision")
        self._store = store
        self._trace = trace
        self._as_of = as_of
        self._as_of_revision = as_of_revision

    def contract_view(self, contract: Contract) -> ContractView:
        historical = self._historical_facts(contract)
        if historical is None:
            assets = self._store.get_all_assets()
            available_names = {asset.name for asset in assets}
            outputs_satisfied = bool(contract.outputs) and all_outputs_satisfied(
                contract, self._store
            )
            entries = self._entries_for(contract.id)
            typed_terminal_events: tuple[str, ...] = ()
            typed_budget: int | None = None
        else:
            asset_payloads, entries, typed_terminal_events, typed_budget = historical
            available_names = {
                str(payload.get("name", "")) for payload in asset_payloads
            }
            outputs_satisfied = bool(contract.outputs) and all(
                any(
                    contract.origin == "system"
                    or is_business_output(
                        SimpleNamespace(
                            name=payload.get("name", ""),
                            origin=payload.get("origin", ""),
                        ),
                        output,
                    )
                    for payload in asset_payloads
                    if payload.get("name") == output
                )
                for output in contract.outputs
            )
        activation_satisfied = check_activation(contract.activation, available_names)
        activation_references = _activation_names(contract.activation)
        required_inputs = set(contract.inputs)
        missing_inputs = required_inputs - available_names
        missing_activation = activation_references - available_names
        missing_assets = tuple(sorted(missing_inputs | missing_activation))
        inputs_satisfied = not missing_inputs
        terminal_events = tuple(
            sorted(
                set(typed_terminal_events)
                | {
                    entry.event_type
                    for entry in entries
                    if entry.event_type in TERMINAL_EVENTS
                }
            )
        )
        if len(terminal_events) > 1:
            terminal = "conflict"
        elif terminal_events:
            terminal = terminal_events[0]
        else:
            terminal = None
        budget_remaining = (
            typed_budget
            if typed_budget is not None
            else _budget_remaining(contract, entries)
        )
        current_claim_id, claim_status = self._claim_view(contract.id)
        method_pending = False
        for entry in entries:
            if entry.event_type in {
                "method_scheduled",
                "method_continuation_scheduled",
            }:
                method_pending = True
            elif entry.event_type == "method_resumed":
                method_pending = False

        blockers: list[str] = []
        if terminal == "conflict":
            blockers.append("terminal_conflict")
        elif terminal is not None:
            blockers.append(f"terminal:{terminal}")
        if outputs_satisfied:
            blockers.append("outputs_satisfied")
        if not inputs_satisfied:
            blockers.extend(f"missing_asset:{name}" for name in sorted(missing_inputs))
        if not activation_satisfied:
            blockers.extend(
                f"missing_asset:{name}"
                for name in sorted(missing_activation)
                if f"missing_asset:{name}" not in blockers
            )
            if not missing_activation:
                blockers.append("activation_unsatisfied")
        if budget_remaining <= 0:
            blockers.append("budget_exhausted")
        if method_pending:
            blockers.append("method_pending")
        if current_claim_id is not None:
            blockers.append(f"claim:{claim_status}")

        canonical = {
            "activation_satisfied": activation_satisfied,
            "blockers": blockers,
            "budget_remaining": budget_remaining,
            "contract_id": contract.id,
            "current_claim_id": current_claim_id,
            "claim_status": claim_status,
            "inputs_satisfied": inputs_satisfied,
            "missing_assets": list(missing_assets),
            "outputs_satisfied": outputs_satisfied,
            "terminal": terminal,
            "terminal_events": list(terminal_events),
        }
        projection_hash = compute_content_hash(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        )
        return ContractView(
            contract_id=contract.id,
            enabled=not blockers,
            blockers=tuple(blockers),
            activation_satisfied=activation_satisfied,
            inputs_satisfied=inputs_satisfied,
            missing_assets=missing_assets,
            outputs_satisfied=outputs_satisfied,
            terminal=terminal,
            terminal_events=terminal_events,
            current_claim_id=current_claim_id,
            claim_status=claim_status,
            budget_remaining=budget_remaining,
            projection_hash=projection_hash,
        )

    def explain_not_enabled(self, contract: Contract) -> tuple[str, ...]:
        """Return stable, structured blockers for audit and CLI views."""
        return self.contract_view(contract).blockers

    def _entries_for(self, contract_id: str) -> list[TraceEntry]:
        entries = self._trace.get_by_contract(contract_id)
        if self._as_of is None:
            return entries
        return [entry for entry in entries if entry.timestamp <= self._as_of]

    def _historical_facts(
        self, contract: Contract
    ) -> tuple[list[dict], list[TraceEntry], tuple[str, ...], int | None] | None:
        if self._as_of is None and self._as_of_revision is None:
            return None
        records = self._store.scan_runtime_records()
        declared = [
            record
            for _, record in records
            if record.record_type == "contract.declared"
            and record.payload["contract"]["id"] == contract.id
        ]
        if not declared:
            # Legacy/import-only stores cannot provide asset history. Preserve
            # trace-only compatibility only when no runtime fact log exists.
            if records:
                raise RuntimeError(
                    f"historical projection for {contract.id!r} lacks contract fact"
                )
            return None

        recorded_asset_ids = {
            record.payload["asset"]["id"]
            for _, record in records
            if record.record_type == "asset.committed"
        }
        current_asset_ids = {asset.id for asset in self._store.get_all_assets()}
        if not current_asset_ids.issubset(recorded_asset_ids):
            raise RuntimeError("historical projection has unrecorded asset facts")

        selected = [
            (revision, record)
            for revision, record in records
            if self._record_selected(revision, record.recorded_at)
        ]
        asset_payloads = [
            dict(record.payload["asset"])
            for _, record in selected
            if record.record_type == "asset.committed"
        ]
        entries = [
            trace_entry_from_dict(dict(record.payload["trace"]))
            for _, record in selected
            if record.record_type == "trace.recorded"
            and record.payload["trace"]["contract_id"] == contract.id
        ]
        terminal_events = tuple(
            str(record.payload["terminal"])
            for _, record in selected
            if record.record_type == "lifecycle.terminal"
            and record.payload["contract_id"] == contract.id
        )
        budget_values = [
            int(record.payload["remaining"])
            for _, record in selected
            if record.record_type == "budget.consumed"
            and record.payload["contract_id"] == contract.id
        ]
        return (
            asset_payloads,
            entries,
            terminal_events,
            budget_values[-1] if budget_values else None,
        )

    def _record_selected(self, revision: int, recorded_at: str) -> bool:
        if self._as_of is None and self._as_of_revision is None:
            return True
        if self._as_of_revision is not None:
            return revision <= self._as_of_revision
        assert self._as_of is not None
        return datetime.fromisoformat(recorded_at) <= datetime.fromisoformat(
            self._as_of
        )

    def _claim_view(self, contract_id: str) -> tuple[str | None, str | None]:
        claim_id: str | None = None
        status: str | None = None
        for revision, record in self._store.scan_runtime_records():
            if not self._record_selected(revision, record.recorded_at):
                continue
            payload = record.payload
            if (
                record.record_type == "claim.granted"
                and payload["contract_id"] == contract_id
            ):
                claim_id = str(payload["claim_id"])
                status = "active"
            elif (
                claim_id is not None
                and record.record_type
                in {"claim.expired", "claim.released", "claim.submitted"}
                and payload["claim_id"] == claim_id
            ):
                status = record.record_type.removeprefix("claim.")
        return claim_id, status


def _activation_names(expression: str) -> set[str]:
    names: set[str] = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", expression or ""):
        if token.upper() not in _ACTIVATION_OPERATORS:
            names.add(token)
    return names


def _budget_remaining(contract: Contract, entries: list[TraceEntry]) -> int:
    initial = max(contract.budget, 1)
    consumed = [
        entry.budget_remaining
        for entry in entries
        if entry.event_type == "budget_consumed"
    ]
    return consumed[-1] if consumed else initial
