"""Read-only decoder for pre-runtime JSONL Asset/Contract history."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aigineering.core.activation import activation_names
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.protocol.types import Asset, Contract


def load_legacy_jsonl_assets(assets_path: str, contracts_path: str) -> dict[str, Asset]:
    """Decode legacy Asset and Contract files in their historical read order.

    Replay uses the Assets by ID, but constructing the old JSONL adapter also
    decoded Contracts. Keep that validation and its ordering as part of the
    read-only compatibility surface.
    """
    assets = _load_assets(assets_path)
    # The old asset loader rebuilt name/creator indexes before it started
    # decoding Contracts. Hashing preserves those native dict-key failures
    # without retaining any writable/indexed Store behavior.
    for asset in assets.values():
        hash(asset.name)
        if asset.created_by:
            hash(asset.created_by)
    contracts = _load_contracts(contracts_path)
    # JsonLStore historically rebuilt in-memory indexes after both files had
    # been decoded. Preserve only the checks that rebuilding performed; replay
    # does not need to materialize the indexes.
    for contract in contracts.values():
        activation_names(contract.activation)
        set(contract.outputs)
    return assets


def _load_assets(path: str) -> dict[str, Asset]:
    assets: dict[str, Asset] = {}
    if not os.path.exists(path):
        return assets
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            asset = Asset(
                id=data["id"],
                name=data["name"],
                content=data["content"],
                content_type=data.get("content_type", "text"),
                created_by=data.get("created_by", ""),
                origin=data.get("origin", ""),
                trust_tier=data.get("trust_tier", "untrusted"),
                minted_by=data.get("minted_by", ""),
                source_uri=data.get("source_uri", ""),
                signed_by=data.get("signed_by", ""),
                signer_kind=data.get("signer_kind", "deterministic"),
                provenance_seal=data.get("provenance_seal", data.get("signature", "")),
                definition_hash=data.get("definition_hash", ""),
                content_hash=data.get("content_hash", ""),
                promptable=data.get("promptable", True),
                disclosure_view=data.get("disclosure_view", "original"),
                keep_flag=data.get("keep_flag", False),
                tombstoned=data.get("tombstoned", False),
                tombstoned_at=data.get("tombstoned_at"),
                lineage_id=data.get("lineage_id", ""),
            )
            _admit_immutable(assets, asset, "asset")
    return assets


def _load_contracts(path: str) -> dict[str, Contract]:
    contracts: dict[str, Contract] = {}
    if not os.path.exists(path):
        return contracts
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            contract = Contract(
                id=data["id"],
                parent_id=data.get("parent_id"),
                name=data.get("name", ""),
                description=data.get("description", ""),
                inputs=data.get("inputs", []),
                outputs=data.get("outputs", []),
                activation=data.get("activation", ""),
                budget=data.get("budget", 0),
                tool_scope=data.get("tool_scope", []),
                labels=data.get("labels", []),
                context_asset_ids=data.get("context_asset_ids", []),
                worker_capabilities=data.get("worker_capabilities", []),
                worker_pools=data.get("worker_pools", []),
                delegation_capabilities=data.get("delegation_capabilities", []),
                delegation_pools=data.get("delegation_pools", []),
                origin=data.get("origin", "human"),
                minting_authority=data.get("minting_authority", []),
                sensitive_input_policy=data.get("sensitive_input_policy"),
                acceptance_policy=data.get("acceptance_policy"),
            )
            _admit_immutable(contracts, contract, "contract")
    return contracts


def _admit_immutable(values: dict, item: Asset | Contract, kind: str) -> None:
    existing = values.get(item.id)
    if existing is not None:
        if existing == item:
            return
        raise ImmutableRecordConflict(kind, item.id)
    values[item.id] = item
