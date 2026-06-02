"""In-memory key-value store for Assets and Contracts."""

from __future__ import annotations

from typing import Optional

from aigineering.protocol.types import Asset, Contract


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
