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
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from aigineering.core.activation import activation_names, check_activation
from aigineering.core.output_satisfaction import (
    all_outputs_satisfied,
    is_business_output,
    is_output_role_asset,
    output_asset_meets_conditions,
)
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


class FactReducer:
    """Pure local consequences of admitted Assets and Task declarations."""

    def __init__(self, store: StoreProtocol, trace: TraceStoreProtocol | None = None):
        self._store = store

    def on_asset_created(self, asset: Asset) -> list[FactReducerEvent]:
        return self.on_assets_created((asset,))

    def on_assets_created(
        self,
        assets: tuple[Asset, ...],
        *,
        pending_contracts: tuple[Contract, ...] = (),
        pending_runtime_records: tuple = (),
        extra_qualified_asset_ids: Mapping[tuple[str, str], set[str]] | None = None,
    ) -> list[FactReducerEvent]:
        by_id = {item.id: item for item in pending_contracts}
        for asset in assets:
            ids = {
                *self._store.get_contracts_declaring_output(asset.name),
                *self._store.get_contracts_waiting_for(asset.name),
                asset.created_by,
            }
            for task_id in ids:
                if (
                    task_id not in by_id
                    and (task := self._store.get_contract(task_id)) is not None
                ):
                    by_id[task_id] = task
        contracts = tuple(by_id[key] for key in sorted(by_id))
        terminal_ids = {
            task.id
            for task in contracts
            if any(
                row.payload.get("terminal") != "failed"
                for row in self._store.read_task_facts(task.id, ("lifecycle.terminal",))
            )
        }
        pending_terminal_ids = {
            row.payload["contract_id"]
            for row in pending_runtime_records
            if row.record_type == "lifecycle.terminal"
        }
        outputs: dict[str, list[Contract]] = {}
        activations: dict[str, list[Contract]] = {}
        for task in contracts:
            for name in set(task.outputs):
                outputs.setdefault(name, []).append(task)
            try:
                names = activation_names(task.activation)
            except (ValueError, RecursionError):
                names = ()
            for name in names:
                activations.setdefault(name, []).append(task)
        available = {asset.name for asset in assets}
        for name in activations:
            if self._store.has_asset_named(name):
                available.add(name)
        events = []
        activated = set()
        for asset in assets:
            source = by_id.get(asset.created_by)
            prefix = next(
                (
                    item
                    for item in METHOD_RESULT_PREFIXES
                    if asset.name.startswith(item)
                ),
                None,
            )
            if prefix and source is not None and asset.name in source.outputs:
                events.append(
                    FactReducerEvent(
                        "method_result_detected",
                        source.id,
                        asset.name,
                        {"prefix": prefix},
                    )
                )
            for task in activations.get(asset.name, ()):
                try:
                    active = check_activation(task.activation, available)
                except (ValueError, RecursionError):
                    active = False
                if task.id not in activated and active:
                    events.append(
                        FactReducerEvent(
                            "activation_active",
                            task.id,
                            asset.name,
                            {"activation": task.activation},
                        )
                    )
                    activated.add(task.id)
            for task in outputs.get(asset.name, ()):
                if (
                    task.id not in terminal_ids
                    and is_output_role_asset(asset, asset.name)
                    and output_asset_meets_conditions(task, asset.name, asset)
                ):
                    events.append(
                        FactReducerEvent("output_satisfied", task.id, asset.name)
                    )
        # All satisfying Tasks are selected before cancelling any descendants.
        completed = []
        for task in contracts:
            if task.id not in terminal_ids and all_outputs_satisfied(
                task,
                self._store,
                pending_assets=assets,
                require_outputs=True,
                extra_qualified_asset_ids=extra_qualified_asset_ids,
            ):
                completed.append(task)
                events.append(
                    FactReducerEvent(
                        "contract_complete",
                        task.id,
                        "",
                        {
                            "trigger": "required_assets_present",
                            "historical_terminal": bool(
                                self._store.read_task_facts(
                                    task.id, ("lifecycle.terminal",)
                                )
                            ),
                        },
                    )
                )
        closed = terminal_ids | pending_terminal_ids | {task.id for task in completed}
        events.extend(
            self._cancel_descendants(
                tuple(completed), assets, closed, pending_contracts
            )
        )
        return events

    def _cancel_descendants(self, completed, assets, closed, pending_contracts):
        pending_children: dict[str, dict[str, Contract]] = {}
        for task in pending_contracts:
            pending_children.setdefault(task.parent_id, {})[task.id] = task
        queue = deque((task, task.id) for task in completed)
        visited = {task.id for task in completed}
        events = []
        while queue:
            parent, cause = queue.popleft()
            children = {
                task.id: task for task in self._store.get_child_contracts(parent.id)
            }
            children.update(pending_children.get(parent.id, {}))
            for child in sorted(children.values(), key=lambda task: task.id):
                if child.id in visited:
                    continue
                visited.add(child.id)
                terminal_facts = self._store.read_task_facts(
                    child.id, ("lifecycle.terminal",)
                )
                terminal = child.id in closed or any(
                    row.payload.get("terminal") != "failed" for row in terminal_facts
                )
                if not terminal and not all_outputs_satisfied(
                    child, self._store, pending_assets=assets, require_outputs=True
                ):
                    events.append(
                        FactReducerEvent(
                            "child_cancelled",
                            child.id,
                            cause,
                            {
                                "parent_id": parent.id,
                                "historical_terminal": bool(terminal_facts),
                            },
                        )
                    )
                queue.append((child, cause))
        return events
