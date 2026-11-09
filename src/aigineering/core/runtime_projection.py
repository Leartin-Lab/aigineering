"""Pure runtime views derived from immutable Contract, Asset, and Trace facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from aigineering.core.activation import (
    activation_names,
    check_activation,
    validate_execution_activation,
)
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.projection_store_view import ProjectionStoreView
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.ids import compute_content_hash
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.store_capabilities import (
    AssetReader,
    RuntimeRecordReader,
    TraceReader,
)
from aigineering.protocol.wire import asset_from_dict

if TYPE_CHECKING:
    from aigineering.protocol.runtime_record import RuntimeRecord
    from aigineering.protocol.types import Contract


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
        # Retain the historical argument shape for callers. Trace is never
        # consulted for task state; durable facts own every control decision.
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
            current_records = tuple(record for _, record in self._records())
            typed_terminal_events = tuple(
                str(record.payload["terminal"])
                for record in current_records
                if record.record_type == "lifecycle.terminal"
                and record.payload.get("contract_id") == contract.id
            )
            allowance_records = current_records
            typed_budget = allowance_available(contract, allowance_records)
        else:
            (
                asset_payloads,
                typed_terminal_events,
                typed_budget,
                historical_records,
            ) = historical
            available_names = {
                str(payload.get("name", "")) for payload in asset_payloads
            }
            outputs_satisfied = all_outputs_satisfied(
                contract,
                ProjectionStoreView(
                    EffectProjectionContext(
                        assets=tuple(asset_from_dict(item) for item in asset_payloads),
                        runtime_records=historical_records,
                    )
                ),
                require_outputs=True,
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
        terminal_events = tuple(sorted(set(typed_terminal_events) - {"failed"}))
        if len(terminal_events) > 1:
            terminal = "conflict"
        elif terminal_events:
            terminal = terminal_events[0]
        else:
            terminal = None
        # Historical failed attempts do not veto later Asset satisfaction.
        # Derive the exceptional old single-terminal case without rewriting it.
        if terminal is None:
            if outputs_satisfied:
                terminal = "complete"
            else:
                if historical is None:
                    projection_store = self._store
                    ancestry_records = current_records
                    get_contract = self._store.get_contract
                else:
                    projection_store = ProjectionStoreView(
                        EffectProjectionContext(
                            assets=tuple(
                                asset_from_dict(item) for item in asset_payloads
                            ),
                            runtime_records=historical_records,
                        )
                    )
                    ancestry_records = historical_records
                    from aigineering.protocol.wire import contract_from_dict

                    ancestors = {
                        row.payload["contract"]["id"]: contract_from_dict(
                            row.payload["contract"]
                        )
                        for row in ancestry_records
                        if row.record_type == "contract.declared"
                    }
                    get_contract = ancestors.get
                parent_id = contract.parent_id
                visited = {contract.id}
                while parent_id and parent_id not in visited:
                    visited.add(parent_id)
                    parent = get_contract(parent_id)
                    if parent is None:
                        break
                    if any(
                        row.record_type == "lifecycle.terminal"
                        and row.payload.get("contract_id") == parent_id
                        and row.payload.get("terminal") == "complete"
                        for row in ancestry_records
                    ) or all_outputs_satisfied(
                        parent, projection_store, require_outputs=True
                    ):
                        terminal = "cancelled"
                        break
                    parent_id = parent.parent_id
        budget_remaining = typed_budget
        current_claim_id, claim_status = self._claim_view(contract.id)
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

    def _historical_facts(
        self, contract: Contract
    ) -> (
        tuple[
            list[dict],
            tuple[str, ...],
            int,
            tuple[RuntimeRecord, ...],
        ]
        | None
    ):
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
            # compatibility only when no runtime fact log exists.
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
        terminal_events = tuple(
            str(record.payload["terminal"])
            for _, record in selected
            if record.record_type == "lifecycle.terminal"
            and record.payload["contract_id"] == contract.id
        )
        selected_records = tuple(record for _, record in selected)
        typed_budget = allowance_available(contract, selected_records)
        return (
            asset_payloads,
            terminal_events,
            typed_budget,
            selected_records,
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
