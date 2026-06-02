"""Disclosure function — determines what assets to reveal to a worker."""

from __future__ import annotations

from typing import Protocol

from aigineering.protocol.types import Asset, Contract


class StoreLike(Protocol):
    def get_all_assets(self) -> list[Asset]: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...


def compute_disclosure(contract: Contract, store: StoreLike) -> list[Asset]:
    if not contract.inputs:
        return store.get_all_assets()

    seen: set[str] = set()
    result: list[Asset] = []

    for input_name in contract.inputs:
        for asset in store.get_assets_by_name(input_name):
            if asset.id not in seen:
                seen.add(asset.id)
                result.append(asset)

    return result
