"""FactReducer — deterministic asset event projection.

Every accepted asset entering the store is a fact.  The FactReducer reads
the current store state and returns a list of structured events describing
what that fact implies: method-result detection, activation readiness,
declared-output satisfaction, contract completion, and cascading child
cancellation.

The reducer is **pure** — it never mutates store, trace, budget, or any
other shared state.  It only reads the store and returns events.  The
caller materializes those events as atomic runtime facts.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from aigineering.core.activation import check_activation
from aigineering.core.output_satisfaction import (
    all_outputs_satisfied,
    is_business_output,
)
from aigineering.core.task_lifecycle import serves_pending_independent_acceptance
from aigineering.protocol.immutability import deep_freeze

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol
    from aigineering.protocol.types import Asset, Contract

# ---------------------------------------------------------------------------
# Method-result asset name prefixes that trigger method completion handling.
# ---------------------------------------------------------------------------

METHOD_RESULT_PREFIXES: tuple[str, ...] = (
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


@dataclass(frozen=True, slots=True)
class _AssetDispatchIndex:
    """Invocation-local immutable lookup view for an Asset batch."""

    contract_by_id: Mapping[str, Contract]
    activation_by_raw_token: Mapping[str, tuple[tuple[int, Contract], ...]]
    outputs_by_name: Mapping[str, tuple[Contract, ...]]
    activation_fallback: tuple[tuple[int, Contract], ...]

    def activation_candidates(self, name: str) -> tuple[Contract, ...]:
        indexed = self.activation_by_raw_token.get(name, ())
        fallback = self.activation_fallback
        merged: list[Contract] = []
        left = right = 0
        while left < len(indexed) and right < len(fallback):
            if indexed[left][0] < fallback[right][0]:
                merged.append(indexed[left][1])
                left += 1
            elif fallback[right][0] < indexed[left][0]:
                merged.append(fallback[right][1])
                right += 1
            else:
                merged.append(indexed[left][1])
                left += 1
                right += 1
        merged.extend(contract for _, contract in indexed[left:])
        merged.extend(contract for _, contract in fallback[right:])
        return tuple(merged)


def _build_asset_dispatch_index(
    contracts: Iterable[Contract], assets: tuple[Asset, ...]
) -> _AssetDispatchIndex:
    """Build one local, multi-value dispatch view in Contract order."""
    del assets  # Batch contents do not alter the immutable Contract relations.
    contract_by_id: dict[str, Contract] = {}
    activation_by_raw_token: dict[str, list[tuple[int, Contract]]] = {}
    outputs_by_name: dict[str, list[Contract]] = {}
    activation_fallback: list[tuple[int, Contract]] = []

    for position, contract in enumerate(contracts):
        contract_by_id[contract.id] = contract
        activation = contract.activation
        if activation:
            if isinstance(activation, str):
                for token in set(activation.split()):
                    activation_by_raw_token.setdefault(token, []).append(
                        (position, contract)
                    )
            else:
                activation_fallback.append((position, contract))
        for output_name in dict.fromkeys(contract.outputs):
            outputs_by_name.setdefault(output_name, []).append(contract)

    return _AssetDispatchIndex(
        contract_by_id=MappingProxyType(contract_by_id),
        activation_by_raw_token=MappingProxyType(
            {token: tuple(owners) for token, owners in activation_by_raw_token.items()}
        ),
        outputs_by_name=MappingProxyType(
            {name: tuple(owners) for name, owners in outputs_by_name.items()}
        ),
        activation_fallback=tuple(activation_fallback),
    )


class FactReducer:
    """Deterministic projection of asset facts into structured events.

    Called by Candidate commitment, claim-bound submission, and compatibility
    ingress for each accepted Asset batch. The reducer reads the current store
    state and returns a flat list of :class:`FactReducerEvent` describing what
    the new facts imply.

    The reducer is **pure** — it never writes to store, trace, budget, or
    any other shared state.  State changes happen in the caller.
    """

    def __init__(
        self, store: StoreProtocol, trace: TraceStoreProtocol | None = None
    ) -> None:
        self._store = store
        # Kept as an optional compatibility argument; reduction reads facts only.
        del trace

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
        dispatch_index = (
            _build_asset_dispatch_index(contracts, assets) if assets else None
        )

        # 1. Method result detection
        for asset in assets:
            events.extend(
                self._detect_method_result(
                    asset,
                    contracts,
                    contract_by_id=(
                        dispatch_index.contract_by_id
                        if dispatch_index is not None
                        else None
                    ),
                )
            )

        # 2. Activation satisfaction
        available_names = (
            {asset.name for asset in self._store.get_all_assets()} if assets else set()
        )
        available_names.update(pending_names)
        activated: set[str] = set()
        activation_results: dict[str, bool] = {}
        for asset in assets:
            assert dispatch_index is not None
            for event in self._detect_activation(
                asset,
                available_names,
                contracts,
                candidate_contracts=dispatch_index.activation_candidates(asset.name),
                activation_results=activation_results,
            ):
                if event.contract_id not in activated:
                    events.append(event)
                    activated.add(event.contract_id)

        # 3. Output satisfaction + contract completion + child cancel
        completed: set[str] = set()
        for asset in assets:
            events.extend(
                self._detect_output_satisfaction(
                    asset,
                    assets,
                    completed,
                    terminal_contract_ids,
                    contracts,
                    candidate_contracts=(
                        dispatch_index.outputs_by_name.get(asset.name, ())
                        if dispatch_index is not None
                        else None
                    ),
                )
            )

        deduplicated: list[FactReducerEvent] = []
        cancelled: set[str] = set()
        for event in events:
            if event.type == "child_cancelled":
                if event.contract_id in cancelled:
                    continue
                cancelled.add(event.contract_id)
            deduplicated.append(event)
        return deduplicated

    def on_contract_completed(
        self, contract: Contract, *, terminal_contract_ids: set[str]
    ) -> list[FactReducerEvent]:
        """Reuse child cancellation for a pending independent completion.

        The caller supplies committed and same-batch terminal IDs so an accepted
        verifier receipt cannot be cancelled while it completes atomically.
        """
        return self._detect_unfinished_children(
            contract, (), terminal_contract_ids, self._contracts_with(())
        )

    # -- Method result detection --------------------------------------------

    def _detect_method_result(
        self,
        asset: Asset,
        contracts: tuple[Contract, ...],
        *,
        contract_by_id: Mapping[str, Contract] | None = None,
    ) -> list[FactReducerEvent]:
        """If *asset* name starts with a method-result prefix, emit an event."""
        for prefix in METHOD_RESULT_PREFIXES:
            if asset.name.startswith(prefix):
                source = (
                    contract_by_id.get(asset.created_by)
                    if contract_by_id is not None
                    else next(
                        (
                            contract
                            for contract in contracts
                            if contract.id == asset.created_by
                        ),
                        None,
                    )
                )
                if source is None or asset.name not in source.outputs:
                    return []
                # Determine which prefix matched (longest match first order)
                matched = _longest_matching_prefix(asset.name, METHOD_RESULT_PREFIXES)
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
        available_names: set[str],
        contracts: tuple[Contract, ...],
        *,
        candidate_contracts: tuple[Contract, ...] | None = None,
        activation_results: dict[str, bool] | None = None,
    ) -> list[FactReducerEvent]:
        """Find contracts whose activation expression references *asset.name*
        and is now satisfied."""
        events: list[FactReducerEvent] = []

        # The caller supplies committed names plus the atomic pending batch.
        for contract in (
            contracts if candidate_contracts is None else candidate_contracts
        ):
            if not contract.activation or not contract.activation.strip():
                continue
            if candidate_contracts is None and not _activation_references(
                contract.activation, asset.name
            ):
                continue
            if activation_results is None:
                active = check_activation(contract.activation, available_names)
            elif contract.id not in activation_results:
                active = check_activation(contract.activation, available_names)
                activation_results[contract.id] = active
            else:
                active = activation_results[contract.id]
            if active:
                events.append(
                    FactReducerEvent(
                        type="activation_active",
                        contract_id=contract.id,
                        asset_name=asset.name,
                        details=MappingProxyType({"activation": contract.activation}),
                    )
                )
        return events

    # -- Output satisfaction ------------------------------------------------

    def _detect_output_satisfaction(
        self,
        asset: Asset,
        pending_assets: tuple[Asset, ...],
        completed: set[str],
        terminal_contract_ids: set[str],
        contracts: tuple[Contract, ...],
        *,
        candidate_contracts: tuple[Contract, ...] | None = None,
    ) -> list[FactReducerEvent]:
        """Find contracts that declare *asset.name* as an output and are
        now fully satisfied."""
        events: list[FactReducerEvent] = []

        for contract in (
            contracts if candidate_contracts is None else candidate_contracts
        ):
            if contract.id in terminal_contract_ids:
                continue
            if asset.name not in contract.outputs:
                continue

            if contract.origin not in {"system", "recovery"} and not is_business_output(
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
                    contract, self._store, pending_assets=pending_assets
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
                        pending_assets,
                        terminal_contract_ids | completed,
                        contracts,
                    )
                )

        return events

    def _detect_unfinished_children(
        self,
        completed_contract: Contract,
        pending_assets: tuple[Asset, ...],
        terminal_contract_ids: set[str],
        contracts: tuple[Contract, ...],
    ) -> list[FactReducerEvent]:
        """Find unfinished descendants to cancel after *completed_contract* ends.

        A terminal or already-satisfied intermediate Contract does not shield
        unfinished descendants from an ancestor that has now closed their
        obligation. A live independent verifier remains an explicit exception.
        """
        events: list[FactReducerEvent] = []
        contracts_by_id = {contract.id: contract for contract in contracts}
        children_by_parent: dict[str, list[Contract]] = {}
        for contract in contracts:
            if contract.parent_id:
                children_by_parent.setdefault(contract.parent_id, []).append(contract)
        for children in children_by_parent.values():
            children.sort(key=lambda item: item.id)

        queue = deque([completed_contract])
        visited = {completed_contract.id}
        known_terminals = set(terminal_contract_ids)
        while queue:
            parent = queue.popleft()
            for child in children_by_parent.get(parent.id, ()):
                if child.id in visited:
                    continue
                visited.add(child.id)
                already_terminal = child.id in known_terminals
                if not already_terminal and self._serves_pending_independent_acceptance(
                    child, parent, contracts_by_id, known_terminals
                ):
                    continue

                # Keep existing terminal facts immutable, but continue through
                # their branch so any still-live descendants can be fenced.
                if already_terminal or self._all_outputs_satisfied(
                    child, pending_assets
                ):
                    queue.append(child)
                    continue

                events.append(
                    FactReducerEvent(
                        type="child_cancelled",
                        contract_id=child.id,
                        asset_name=completed_contract.id,
                        details=MappingProxyType({"parent_id": parent.id}),
                    )
                )
                known_terminals.add(child.id)
                queue.append(child)
        return events

    @staticmethod
    def _serves_pending_independent_acceptance(
        child: Contract,
        completed_parent: Contract,
        contracts_by_id: dict[str, Contract],
        terminal_contract_ids: set[str],
    ) -> bool:
        """Keep an explicit verifier reachable after an intermediate parent ends.

        Staged planning owns its compiled children, so the task producing a
        business output and the task independently qualifying it are siblings.
        The compiler may finish when the output appears, but its verifier still
        serves an independently accepted ancestor.  Capability and input
        containment make that relationship explicit in immutable Contracts.
        """
        return serves_pending_independent_acceptance(
            child,
            completed_parent,
            contracts_by_id,
            terminal_contract_ids,
        )

    def _contracts_with(
        self, pending_contracts: tuple[Contract, ...]
    ) -> tuple[Contract, ...]:
        """Return the transaction-view Contract set without duplicate identities."""
        by_id = {contract.id: contract for contract in self._store.get_all_contracts()}
        by_id.update((contract.id, contract) for contract in pending_contracts)
        return tuple(by_id[contract_id] for contract_id in sorted(by_id))

    # -- Helpers ------------------------------------------------------------

    def _all_outputs_satisfied(
        self, contract: Contract, pending_assets: tuple[Asset, ...] = ()
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
            pending_assets=pending_assets,
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
