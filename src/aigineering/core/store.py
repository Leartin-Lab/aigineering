"""In-memory and persistent key-value stores for Assets and Contracts."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict, contract_to_dict

_logger = logging.getLogger(__name__)


@runtime_checkable
class StoreProtocol(Protocol):
    """Protocol that any store (in-memory or persistent) must satisfy."""

    def add_asset(self, asset: Asset) -> None: ...
    def get_asset(self, asset_id: str) -> Optional[Asset]: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...
    def has_asset_named(self, name: str) -> bool: ...
    def get_all_assets(self) -> list[Asset]: ...
    def add_contract(self, contract: Contract) -> None: ...
    def get_contract(self, contract_id: str) -> Optional[Contract]: ...
    def get_all_contracts(self) -> list[Contract]: ...
    def get_assets_by_contract(self, contract_id: str) -> list[Asset]: ...


class MemoryStore:
    def __init__(self) -> None:
        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, Contract] = {}

    def add_asset(self, asset: Asset) -> None:
        self.assets[asset.id] = asset

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        return self.assets.get(asset_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.name == name]

    def has_asset_named(self, name: str) -> bool:
        return any(a.name == name for a in self.assets.values())

    def get_all_assets(self) -> list[Asset]:
        return list(self.assets.values())

    def add_contract(self, contract: Contract) -> None:
        self.contracts[contract.id] = contract

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        return self.contracts.get(contract_id)

    def get_all_contracts(self) -> list[Contract]:
        return list(self.contracts.values())

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.created_by == contract_id]


class JsonLStore:
    """Persistent JSONL store for Assets and Contracts — one JSON object per line."""

    def __init__(self, assets_path: str, contracts_path: str) -> None:
        self._assets_path = assets_path
        self._contracts_path = contracts_path

        for p in (assets_path, contracts_path):
            parent = Path(p).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, Contract] = {}
        self._name_index: dict[str, list[str]] = {}
        self._created_by_index: dict[str, list[str]] = {}

        self._load_assets()
        self._load_contracts()

    def _load_assets(self) -> None:
        if not os.path.exists(self._assets_path):
            return
        with open(self._assets_path, "r", encoding="utf-8") as f:
            for line in f:
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
                    origin=data.get("origin", "system"),
                )
                self.assets[asset.id] = asset
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._name_index.clear()
        self._created_by_index.clear()
        for asset in self.assets.values():
            self._name_index.setdefault(asset.name, []).append(asset.id)
            if asset.created_by:
                self._created_by_index.setdefault(asset.created_by, []).append(asset.id)

    def _load_contracts(self) -> None:
        if not os.path.exists(self._contracts_path):
            return
        with open(self._contracts_path, "r", encoding="utf-8") as f:
            for line in f:
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
                    origin=data.get("origin", "human"),
                )
                self.contracts[contract.id] = contract

    @staticmethod
    def _write_jsonl_line(path: str, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                _logger.warning("fsync failed for %s", path)

    def add_asset(self, asset: Asset) -> None:
        line = json.dumps(asset_to_dict(asset), ensure_ascii=False) + "\n"
        self._write_jsonl_line(self._assets_path, line)
        # If ID already exists, remove old index entries before overwriting
        existing = self.assets.get(asset.id)
        if existing is not None:
            if existing.name in self._name_index:
                self._name_index[existing.name] = [
                    aid for aid in self._name_index[existing.name] if aid != asset.id
                ]
            if existing.created_by and existing.created_by in self._created_by_index:
                self._created_by_index[existing.created_by] = [
                    aid for aid in self._created_by_index[existing.created_by] if aid != asset.id
                ]
        self.assets[asset.id] = asset
        self._name_index.setdefault(asset.name, []).append(asset.id)
        if asset.created_by:
            self._created_by_index.setdefault(asset.created_by, []).append(asset.id)

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        return self.assets.get(asset_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        ids = self._name_index.get(name, [])
        return [self.assets[aid] for aid in ids if aid in self.assets]

    def has_asset_named(self, name: str) -> bool:
        return name in self._name_index and len(self._name_index[name]) > 0

    def get_all_assets(self) -> list[Asset]:
        return list(self.assets.values())

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        ids = self._created_by_index.get(contract_id, [])
        return [self.assets[aid] for aid in ids if aid in self.assets]

    def add_contract(self, contract: Contract) -> None:
        line = json.dumps(contract_to_dict(contract), ensure_ascii=False) + "\n"
        self._write_jsonl_line(self._contracts_path, line)
        self.contracts[contract.id] = contract

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        return self.contracts.get(contract_id)

    def get_all_contracts(self) -> list[Contract]:
        return list(self.contracts.values())
