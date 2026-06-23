"""Engine state serialization and trace-based reconstruction helpers."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.core.budget_manager import BudgetManager
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import TraceStoreProtocol
from aigineering.core.trace_manager import TraceManager
from aigineering.protocol.types import Asset, Contract


@dataclass(frozen=True)
class EngineState:
    """Snapshot of mutable Engine state required for recovery."""

    budget: dict[str, int]
    completed: set[str]
    suspended: set[str]
    method_scheduled: set[str]
    method_context: dict[str, list[Asset]]
    label_context: dict[str, list[Asset]]
    contract_last_entry: dict[str, str]


class StateSerializer:
    """Serialize and deserialize Engine runtime state."""

    @staticmethod
    def serialize(
        budget_mgr: BudgetManager,
        completed: set[str],
        suspended: set[str],
        method_scheduled: set[str],
        method_context: dict[str, list[Asset]],
        label_context: dict[str, list[Asset]],
        trace_mgr: TraceManager,
    ) -> dict:
        """Serialize mutable Engine state to a JSON-compatible dict."""
        return {
            "budget": budget_mgr.get_all(),
            "completed": list(completed),
            "suspended": list(suspended),
            "method_scheduled": list(method_scheduled),
            "method_context": {
                key: [asset.id for asset in assets]
                for key, assets in method_context.items()
            },
            "label_context": {
                key: [asset.id for asset in assets]
                for key, assets in label_context.items()
            },
            "contract_last_entry": trace_mgr.get_all_last_entries(),
        }

    @staticmethod
    def deserialize(store: StoreProtocol, data: dict) -> EngineState:
        """Deserialize state, resolving asset references against the store."""
        return EngineState(
            budget=dict(data["budget"]),
            completed=set(data["completed"]),
            suspended=set(data["suspended"]),
            method_scheduled=set(data["method_scheduled"]),
            method_context={
                key: [
                    asset
                    for asset_id in asset_ids
                    if (asset := store.get_asset(asset_id)) is not None
                ]
                for key, asset_ids in data["method_context"].items()
            },
            label_context={
                key: [
                    asset
                    for asset_id in asset_ids
                    if (asset := store.get_asset(asset_id)) is not None
                ]
                for key, asset_ids in data["label_context"].items()
            },
            contract_last_entry=dict(data["contract_last_entry"]),
        )


class TraceStateRebuilder:
    """Rebuild EngineState from persisted contracts and trace events."""

    @staticmethod
    def rebuild(store: StoreProtocol, trace_store: TraceStoreProtocol) -> EngineState:
        """Reconstruct Engine state from store records and trace events."""
        consumption_counts: dict[str, int] = {}
        completed: set[str] = set()
        suspended: set[str] = set()
        method_scheduled: set[str] = set()
        method_context: dict[str, list[Asset]] = {}
        label_context: dict[str, list[Asset]] = {}
        contract_last_entry: dict[str, str] = {}

        for entry in trace_store.get_all():
            cid = entry.contract_id

            if entry.event_type == "budget_consumed":
                consumption_counts[cid] = consumption_counts.get(cid, 0) + 1
            elif entry.event_type == "method_scheduled":
                suspended.add(cid)
                if entry.relation_target:
                    method_scheduled.add(entry.relation_target)
            elif entry.event_type == "complete":
                completed.add(cid)
                suspended.discard(cid)
            elif entry.event_type == "method_resumed":
                suspended.discard(cid)
                assets = _resolve_assets(store, entry.disclosed_assets)
                if assets:
                    method_context.setdefault(cid, []).extend(assets)
            elif entry.event_type == "method_continuation_scheduled":
                if entry.relation_target:
                    method_scheduled.add(entry.relation_target)
                assets = _resolve_assets(store, entry.disclosed_assets)
                if assets and entry.relation_target:
                    method_context.setdefault(entry.relation_target, []).extend(assets)
            elif entry.event_type == "label_resolved":
                assets = _resolve_assets(store, entry.disclosed_assets)
                if assets:
                    label_context[cid] = assets

            contract_last_entry[cid] = entry.id

        budget = _derive_budget(store.get_all_contracts(), consumption_counts)
        return EngineState(
            budget=budget,
            completed=completed,
            suspended=suspended,
            method_scheduled=method_scheduled,
            method_context=method_context,
            label_context=label_context,
            contract_last_entry=contract_last_entry,
        )


def _resolve_assets(store: StoreProtocol, asset_ids: list[str]) -> list[Asset]:
    return [
        asset
        for asset_id in asset_ids
        if (asset := store.get_asset(asset_id)) is not None
    ]


def _derive_budget(
    contracts: list[Contract], consumption_counts: dict[str, int]
) -> dict[str, int]:
    budget: dict[str, int] = {}
    for contract in contracts:
        initial = max(contract.budget, 1)
        consumed = consumption_counts.get(contract.id, 0)
        budget[contract.id] = max(0, initial - consumed)
    return budget


# ---------------------------------------------------------------------------
# Durable lifecycle derivation from store records (Phase E)
# ---------------------------------------------------------------------------


def _all_outputs_present(contract: Contract, store: StoreProtocol) -> bool:
    """Return True when all declared outputs of *contract* exist in the store
    AND are business outputs (not tool/MCP observations).

    This mirrors :meth:`FactReducer._all_outputs_satisfied` and uses the same
    store-level check with source class filtering.
    """
    return all_outputs_satisfied(contract, store, require_outputs=True)


def _derive_budget_from_contracts(store: StoreProtocol) -> dict[str, int]:
    """Derive initial budget state from contracts (without consumption tracking).

    Consumption data lives in TraceStore events (``budget_consumed``), not in
    the durable store.  Callers should overlay trace-derived budget for
    accurate remaining-credit figures.
    """
    budget: dict[str, int] = {}
    for contract in store.get_all_contracts():
        budget[contract.id] = max(contract.budget, 1)
    return budget


def derive_lifecycle_from_store(store: StoreProtocol) -> EngineState:
    """Derive Engine lifecycle state from durable store facts.

    Completion is determined by output satisfaction: a contract is
    **complete** when all its declared outputs exist as assets in the store.
    This is the same logic as :meth:`FactReducer._all_outputs_satisfied`.

    Suspended contracts are those with active method children, detectable
    via ``_method_ctx_`` assets in the store.

    Method-scheduled contracts are contracts whose activation references an
    existing ``_method_ctx_`` asset.

    Budget is derived from contract declarations only (no consumption info);
    callers should overlay trace-derived budget for accurate remaining-credit.
    """
    completed: set[str] = set()
    suspended: set[str] = set()
    method_scheduled: set[str] = set()

    all_asset_names: set[str] = {a.name for a in store.get_all_assets()}

    for contract in store.get_all_contracts():
        # Completion via output satisfaction
        if _all_outputs_present(contract, store):
            completed.add(contract.id)

        # Method-scheduled: contracts whose activation references an
        # existing _method_ctx_ asset
        if (
            contract.activation
            and contract.activation.startswith("_method_ctx_")
            and contract.activation in all_asset_names
        ):
            method_scheduled.add(contract.id)

    # Suspended: contracts with active method children (have _method_ctx_ assets)
    for name in all_asset_names:
        if name.startswith("_method_ctx_"):
            parent_id = name[len("_method_ctx_"):]
            if parent_id:
                suspended.add(parent_id)

    budget = _derive_budget_from_contracts(store)

    return EngineState(
        budget=budget,
        completed=completed,
        suspended=suspended,
        method_scheduled=method_scheduled,
        method_context={},
        label_context={},
        contract_last_entry={},
    )
