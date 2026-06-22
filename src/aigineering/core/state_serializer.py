"""Engine state serialization and trace-based reconstruction helpers."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.core.budget_manager import BudgetManager
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
