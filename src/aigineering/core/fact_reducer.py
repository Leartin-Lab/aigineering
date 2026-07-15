"""FactReducer — deterministic asset event projection.

Every accepted asset entering the store is a fact.  The FactReducer reads
the current store state and returns a list of structured events describing
what that fact implies: method-result detection, activation readiness,
declared-output satisfaction, contract completion, and cascading child
cancellation.

The reducer is **pure** — it never mutates store, trace, budget, or any
other shared state.  It only reads the store and returns events.  The
caller materializes those events as atomic runtime facts.

References: W1 (Fact Ingress And Reactive Reducer) of
``.omo/plans/050-runtime-boundary-refactor-plan.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from aigineering.core.activation import check_activation
from aigineering.core.output_satisfaction import (
    all_outputs_satisfied,
    is_business_output,
)
from aigineering.protocol.immutability import deep_freeze

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol
    from aigineering.protocol.types import Asset, Contract

# ---------------------------------------------------------------------------
# Method-result asset name prefixes that trigger method completion handling.
# ---------------------------------------------------------------------------

_METHOD_RESULT_PREFIXES: tuple[str, ...] = (
    "_plan_result_",
    "_replan_result_",
    "_fail_result_",
    "_sufficiency_result_",
    "_file_content_",
)

# ---------------------------------------------------------------------------
# FactReducerEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactReducerEvent:
    """A structured event produced by :class:`FactReducer` in response to a
    new asset entering the store.

    Each event describes one consequence of the asset: method-result
    detection, activation becoming active, output satisfaction, contract
    completion, or child cancellation.
    """

    type: str
    """Event category:

    - ``"method_result_detected"`` — the asset is a recognised method-result
      asset.
    - ``"activation_active"`` — the asset name appears in the contract's
      activation expression and the expression is now true.
    - ``"output_satisfied"`` — the asset name matches a declared output of
      the contract.
    - ``"contract_complete"`` — all declared outputs of the contract are
      present in the store.
    - ``"child_cancelled"`` — an unfinished child of a completed parent
      should be cancelled.
    """

    contract_id: str
    """The contract affected by this event."""

    asset_name: str
    """The asset name that triggered this event."""

    details: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    """Additional event-specific metadata."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", deep_freeze(self.details))


# ---------------------------------------------------------------------------
# FactReducer
# ---------------------------------------------------------------------------


# Backward-compatible alias for tests or external users that imported the
# previous private helper.  New runtime code should use output_satisfaction.
_is_business_output = is_business_output


class FactReducer:
    """Deterministic projection of asset facts into structured events.

    Called by Candidate commitment, claim-bound submission, and compatibility
    ingress for each accepted Asset batch. The reducer reads the current store
    state and returns a flat list of :class:`FactReducerEvent` describing what
    the new facts imply.

    The reducer is **pure** — it never writes to store, trace, budget, or
    any other shared state.  State changes happen in the caller.
    """

    def __init__(self, store: StoreProtocol, trace: TraceStoreProtocol) -> None:
        self._store = store
        self._trace = trace

    # -- Public API ---------------------------------------------------------

    def on_asset_created(self, asset: Asset) -> list[FactReducerEvent]:
        """Project the consequences of *asset* entering the store.

        Returns a deterministic list of events.  The caller applies them.
        """
        return self.on_assets_created((asset,))

    def on_assets_created(
        self,
        assets: tuple[Asset, ...],
        *,
        pending_contracts: tuple[Contract, ...] = (),
    ) -> list[FactReducerEvent]:
        """Project one atomic batch without depending on insertion order."""
        events: list[FactReducerEvent] = []
        pending_names = {asset.name for asset in assets}
        contracts = self._contracts_with(pending_contracts)
        terminal_contract_ids = {
            str(record.payload.get("contract_id", ""))
            for _, record in self._store.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
        }

        # 1. Method result detection
        for asset in assets:
            events.extend(self._detect_method_result(asset))

        # 2. Activation satisfaction
        activated: set[str] = set()
        for asset in assets:
            for event in self._detect_activation(asset, pending_names, contracts):
                if event.contract_id not in activated:
                    events.append(event)
                    activated.add(event.contract_id)

        # 3. Output satisfaction + contract completion + child cancel
        completed: set[str] = set()
        for asset in assets:
            events.extend(
                self._detect_output_satisfaction(
                    asset, pending_names, completed, terminal_contract_ids, contracts
                )
            )

        return events

    # -- Method result detection --------------------------------------------

    def _detect_method_result(self, asset: Asset) -> list[FactReducerEvent]:
        """If *asset* name starts with a method-result prefix, emit an event."""
        for prefix in _METHOD_RESULT_PREFIXES:
            if asset.name.startswith(prefix):
                # Determine which prefix matched (longest match first order)
                matched = _longest_matching_prefix(asset.name, _METHOD_RESULT_PREFIXES)
                return [
                    FactReducerEvent(
                        type="method_result_detected",
                        contract_id=asset.created_by,
                        asset_name=asset.name,
                        details=MappingProxyType({"prefix": matched}),
                    )
                ]
        return []

    # -- Activation detection -----------------------------------------------

    def _detect_activation(
        self,
        asset: Asset,
        pending_names: set[str],
        contracts: tuple[Contract, ...],
    ) -> list[FactReducerEvent]:
        """Find contracts whose activation expression references *asset.name*
        and is now satisfied."""
        events: list[FactReducerEvent] = []

        # Build the set of currently available asset names (after this asset).
        available_names: set[str] = {a.name for a in self._store.get_all_assets()}
        # SQLite computes reducer consequences inside the transaction before
        # physically inserting the new fact; MemoryStore computes after. Make
        # the reducer's transaction view explicit so both adapters agree.
        available_names.update(pending_names)

        # The new asset is already in the store, so its name is included.
        for contract in contracts:
            if not contract.activation or not contract.activation.strip():
                continue
            # Only report activation if the expression references this asset.
            if _activation_references(contract.activation, asset.name):
                if check_activation(contract.activation, available_names):
                    events.append(
                        FactReducerEvent(
                            type="activation_active",
                            contract_id=contract.id,
                            asset_name=asset.name,
                            details=MappingProxyType(
                                {"activation": contract.activation}
                            ),
                        )
                    )
        return events

    # -- Output satisfaction ------------------------------------------------

    def _detect_output_satisfaction(
        self,
        asset: Asset,
        pending_names: set[str],
        completed: set[str],
        terminal_contract_ids: set[str],
        contracts: tuple[Contract, ...],
    ) -> list[FactReducerEvent]:
        """Find contracts that declare *asset.name* as an output and are
        now fully satisfied."""
        events: list[FactReducerEvent] = []

        for contract in contracts:
            if contract.id in terminal_contract_ids:
                continue
            if asset.name not in contract.outputs:
                continue

            if contract.origin != "system" and not is_business_output(
                asset, asset.name
            ):
                continue

            events.append(
                FactReducerEvent(
                    type="output_satisfied",
                    contract_id=contract.id,
                    asset_name=asset.name,
                )
            )

            # Check full output satisfaction.
            if (
                all_outputs_satisfied(
                    contract, self._store, extra_output_names=pending_names
                )
                and contract.id not in completed
            ):
                completed.add(contract.id)
                events.append(
                    FactReducerEvent(
                        type="contract_complete",
                        contract_id=contract.id,
                        asset_name=asset.name,
                        details=MappingProxyType({"trigger": "output_satisfied"}),
                    )
                )
                # For newly-completed contracts, identify unfinished children.
                events.extend(
                    self._detect_unfinished_children(
                        contract,
                        pending_names,
                        terminal_contract_ids | completed,
                        contracts,
                    )
                )

        return events

    def _detect_unfinished_children(
        self,
        completed_contract: Contract,
        pending_names: set[str],
        terminal_contract_ids: set[str],
        contracts: tuple[Contract, ...],
    ) -> list[FactReducerEvent]:
        """Find child contracts that should be cancelled after *completed_contract*
        finishes."""
        events: list[FactReducerEvent] = []

        for contract in contracts:
            if contract.parent_id != completed_contract.id:
                continue
            if contract.id in terminal_contract_ids:
                continue
            # If the child still has outstanding outputs, it's unfinished.
            if not self._all_outputs_satisfied(contract, pending_names):
                events.append(
                    FactReducerEvent(
                        type="child_cancelled",
                        contract_id=contract.id,
                        asset_name=completed_contract.id,
                        details=MappingProxyType({"parent_id": completed_contract.id}),
                    )
                )
        return events

    def _contracts_with(
        self, pending_contracts: tuple[Contract, ...]
    ) -> tuple[Contract, ...]:
        """Return the transaction-view Contract set without duplicate identities."""
        by_id = {contract.id: contract for contract in self._store.get_all_contracts()}
        by_id.update((contract.id, contract) for contract in pending_contracts)
        return tuple(by_id[contract_id] for contract_id in sorted(by_id))

    # -- Helpers ------------------------------------------------------------

    def _all_outputs_satisfied(
        self, contract: Contract, pending_names: set[str] | None = None
    ) -> bool:
        """Return True when all declared outputs of *contract* exist in
        the store AND are business outputs (not tool/MCP observations).

        System method contracts (origin="system") intentionally produce
        observation-like assets as their declared outputs, so source class
        filtering is only applied to non-system contracts.
        """
        return all_outputs_satisfied(
            contract,
            self._store,
            extra_output_names=pending_names or set(),
        )


# ---------------------------------------------------------------------------
# Module-level helpers (usable without a FactReducer instance)
# ---------------------------------------------------------------------------


def _activation_references(expression: str, name: str) -> bool:
    """Return True when *name* appears as a token in the activation
    expression."""
    tokens = expression.split()
    return name in tokens


def _longest_matching_prefix(name: str, prefixes: tuple[str, ...]) -> str:
    """Return the longest prefix from *prefixes* that matches *name*."""
    best = ""
    for prefix in prefixes:
        if name.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return best
