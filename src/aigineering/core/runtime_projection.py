"""Pure runtime views derived from immutable Contract, Asset, and Trace facts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigineering.core.activation import check_activation
from aigineering.core.ids import compute_content_hash
from aigineering.core.output_satisfaction import all_outputs_satisfied

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol
    from aigineering.protocol.types import Contract, TraceEntry


_TERMINAL_EVENTS = frozenset({"complete", "failed", "cancelled", "unreachable"})
_ACTIVATION_OPERATORS = frozenset({"AND", "OR", "NOT"})


@dataclass(frozen=True)
class ContractView:
    """Deterministic execution view for one immutable Contract."""

    contract_id: str
    enabled: bool
    blockers: tuple[str, ...]
    activation_satisfied: bool
    missing_assets: tuple[str, ...]
    outputs_satisfied: bool
    terminal: str | None
    terminal_events: tuple[str, ...]
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
    ) -> None:
        self._store = store
        self._trace = trace
        self._as_of = as_of

    def contract_view(self, contract: Contract) -> ContractView:
        assets = self._store.get_all_assets()
        available_names = {asset.name for asset in assets}
        activation_satisfied = check_activation(contract.activation, available_names)
        referenced = _activation_names(contract.activation)
        missing_assets = tuple(sorted(referenced - available_names))
        outputs_satisfied = bool(contract.outputs) and all_outputs_satisfied(
            contract, self._store
        )
        entries = self._entries_for(contract.id)
        terminal_events = tuple(
            sorted(
                {
                    entry.event_type
                    for entry in entries
                    if entry.event_type in _TERMINAL_EVENTS
                }
            )
        )
        if len(terminal_events) > 1:
            terminal = "conflict"
        elif terminal_events:
            terminal = terminal_events[0]
        else:
            terminal = None
        budget_remaining = _budget_remaining(contract, entries)

        blockers: list[str] = []
        if terminal == "conflict":
            blockers.append("terminal_conflict")
        elif terminal is not None:
            blockers.append(f"terminal:{terminal}")
        if outputs_satisfied:
            blockers.append("outputs_satisfied")
        if not activation_satisfied:
            blockers.extend(f"missing_asset:{name}" for name in missing_assets)
            if not missing_assets:
                blockers.append("activation_unsatisfied")
        if budget_remaining <= 0:
            blockers.append("budget_exhausted")

        canonical = {
            "activation_satisfied": activation_satisfied,
            "blockers": blockers,
            "budget_remaining": budget_remaining,
            "contract_id": contract.id,
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
            missing_assets=missing_assets,
            outputs_satisfied=outputs_satisfied,
            terminal=terminal,
            terminal_events=terminal_events,
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
