"""Pure runtime views derived from immutable Contract, Asset, and Trace facts."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol

from aigineering.core.activation import (
    activation_names,
    check_activation,
    validate_execution_activation,
)
from aigineering.core.claim_holds import open_claim_holds
from aigineering.core.claim_constraints import claim_constraint_blockers
from aigineering.core.claim_assignments import (
    assignment_blocker,
    assignment_context_from_projection,
    effective_assignment,
)
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.worker_routing import registration_from_record
from aigineering.core.causal_allowance import (
    allowance_available,
    allowance_is_recorded,
)
from aigineering.core.ids import compute_content_hash
from aigineering.core.output_satisfaction import (
    all_outputs_satisfied,
    is_business_output,
)
from aigineering.core.store_capabilities import (
    AssetReader,
    RuntimeRecordReader,
    TraceReader,
)
from aigineering.protocol.wire import trace_entry_from_dict

if TYPE_CHECKING:
    from aigineering.protocol.runtime_record import RuntimeRecord
    from aigineering.protocol.types import Contract, TraceEntry


TERMINAL_EVENTS = frozenset({"complete", "failed", "cancelled", "unreachable"})


class RuntimeProjectionReader(AssetReader, RuntimeRecordReader, Protocol):
    """Read capabilities required to derive runtime task views."""


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
    assignment_worker_id: str | None = None
    assignment_blocker: str | None = None


class RuntimeProjection:
    """Project runtime state without mutating Engine or persistence."""

    def __init__(
        self,
        store: RuntimeProjectionReader,
        trace: TraceReader,
        *,
        as_of: str | None = None,
        as_of_revision: int | None = None,
        runtime_records: tuple[tuple[int, RuntimeRecord], ...] | None = None,
        available_names: set[str] | frozenset[str] | None = None,
    ) -> None:
        if as_of is not None and as_of_revision is not None:
            raise ValueError("choose either as_of timestamp or as_of_revision")
        self._store = store
        self._trace = trace
        self._as_of = as_of
        self._as_of_revision = as_of_revision
        self._runtime_records = runtime_records
        self._available_names = (
            frozenset(available_names) if available_names is not None else None
        )

    def contract_view(self, contract: Contract) -> ContractView:
        historical = self._historical_facts(contract)
        if historical is None:
            if self._available_names is None:
                assets = self._store.get_all_assets()
                available_names = {asset.name for asset in assets}
            else:
                available_names = self._available_names
            outputs_satisfied = bool(contract.outputs) and all_outputs_satisfied(
                contract, self._store
            )
            entries = self._entries_for(contract.id)
            current_records = tuple(record for _, record in self._records())
            typed_terminal_events = tuple(
                str(record.payload["terminal"])
                for record in current_records
                if record.record_type == "lifecycle.terminal"
                and record.payload.get("contract_id") == contract.id
            )
            allowance_records = current_records
            typed_budget = (
                allowance_available(contract, allowance_records)
                if allowance_is_recorded(contract.id, allowance_records)
                else None
            )
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
        invalid_activation = False
        try:
            validate_execution_activation(contract.activation)
            activation_satisfied = check_activation(
                contract.activation, available_names
            )
            activation_references = activation_names(contract.activation)
        except (RecursionError, ValueError):
            invalid_activation = True
            activation_satisfied = False
            activation_references = set()
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
        claim_holds = open_claim_holds(
            (
                record
                for revision, record in self._records()
                if self._record_selected(revision, record.recorded_at)
            ),
            contract_id=contract.id,
        )
        delegation_pending = False
        for entry in entries:
            if entry.event_type in {
                "task_delegated",
                "method_scheduled",
                "method_continuation_scheduled",
            }:
                delegation_pending = True
            elif entry.event_type == "method_resumed":
                delegation_pending = False

        selected_records = tuple(
            record
            for revision, record in self._records()
            if self._record_selected(revision, record.recorded_at)
        )
        constraint_blockers = claim_constraint_blockers(
            selected_records, contract_id=contract.id
        )
        assignment_context = _assignment_context(contract, selected_records)
        assignment = effective_assignment(assignment_context, contract_id=contract.id)
        assigned_worker_id = (
            str(assignment.payload["worker_id"]) if assignment is not None else None
        )
        assignment_issue = assignment_blocker(
            assignment_context, contract_id=contract.id
        )
        assignment_blocker_code = (
            str(assignment_issue["reason"]) if assignment_issue else None
        )
        blockers: list[str] = []
        if terminal == "conflict":
            blockers.append("terminal_conflict")
        elif terminal is not None:
            blockers.append(f"terminal:{terminal}")
        else:
            if outputs_satisfied:
                blockers.append("outputs_satisfied")
            if not inputs_satisfied:
                blockers.extend(
                    f"missing_asset:{name}" for name in sorted(missing_inputs)
                )
            if invalid_activation:
                blockers.append("invalid_activation")
            elif not activation_satisfied:
                blockers.extend(
                    f"missing_asset:{name}"
                    for name in sorted(missing_activation)
                    if f"missing_asset:{name}" not in blockers
                )
                if not missing_activation:
                    blockers.append("activation_unsatisfied")
            if budget_remaining <= 0:
                blockers.append("budget_exhausted")
            if delegation_pending:
                blockers.append("delegation_pending")
            if assignment_issue is not None:
                blockers.append(
                    f"assignment:{assignment_issue['reason']}:{assignment_issue['worker_id']}"
                )
            if current_claim_id is not None:
                blockers.append(f"claim:{claim_status}")
            blockers.extend(f"claim_hold:{record.id}" for record in claim_holds)
            blockers.extend(
                "prerequisite:"
                f"{item['constraint_id']}:"
                f"{item['prerequisite_contract_id']}:"
                f"{item['observed_terminal']}"
                for item in constraint_blockers
            )

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
        if assignment is not None:
            canonical["assignment_worker_id"] = assigned_worker_id
            canonical["assignment_blocker"] = assignment_blocker_code
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
            assignment_worker_id=assigned_worker_id,
            assignment_blocker=assignment_blocker_code,
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
        records = self._records()
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
        selected_records = tuple(record for _, record in selected)
        typed_budget = (
            allowance_available(contract, selected_records)
            if allowance_is_recorded(contract.id, selected_records)
            else None
        )
        return (
            asset_payloads,
            entries,
            terminal_events,
            typed_budget,
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
        for revision, record in self._records():
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

    def _records(self) -> tuple[tuple[int, RuntimeRecord], ...]:
        if self._runtime_records is not None:
            return self._runtime_records
        return tuple(self._store.scan_runtime_records())


def _budget_remaining(contract: Contract, entries: list[TraceEntry]) -> int:
    initial = max(contract.budget, 1)
    consumed = [
        entry.budget_remaining
        for entry in entries
        if entry.event_type == "budget_consumed"
    ]
    return consumed[-1] if consumed else initial


def _assignment_context(contract, records):
    """Build the assignment read model from the same selected fact cutoff."""
    latest: dict[str, RuntimeRecord] = {}
    active_claims: dict[str, str] = {}
    for record in records:
        if record.record_type == "worker.registered":
            latest[str(record.payload.get("worker_id", ""))] = record
        elif record.record_type == "claim.granted":
            active_claims[str(record.payload.get("claim_id", ""))] = str(
                record.payload.get("worker_id", "")
            )
        elif record.record_type in {
            "claim.expired",
            "claim.released",
            "claim.submitted",
        }:
            active_claims.pop(str(record.payload.get("claim_id", "")), None)
    counts: dict[str, int] = {}
    for worker_id in active_claims.values():
        counts[worker_id] = counts.get(worker_id, 0) + 1
    registrations = tuple(
        replace(
            registration_from_record(record),
            active_claims=counts.get(worker_id, 0),
        )
        for worker_id, record in sorted(latest.items())
        if worker_id
    )
    return assignment_context_from_projection(
        EffectProjectionContext(
            contracts=(contract,),
            runtime_records=tuple(records),
            registrations=registrations,
        )
    )
