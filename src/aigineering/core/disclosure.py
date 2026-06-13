"""Disclosure function — determines what assets to reveal to a worker."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from aigineering.protocol.types import Asset, Contract

REDACTED_CONTENT = "[redacted]"


class StoreLike(Protocol):
    def get_all_assets(self) -> list[Asset]: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...


def redact_for_disclosure(asset: Asset) -> Asset:
    """Return a redacted copy of *asset* when its disclosure_view is not 'original'.

    The returned copy has ``content == REDACTED_CONTENT`` but preserves
    the original ``id``, ``content_hash``, and ``signature`` — they identify
    the stored asset, not the disclosure view.
    """
    if asset.disclosure_view == "original":
        return asset
    return replace(asset, content=REDACTED_CONTENT)


def compute_disclosure(contract: Contract, store: StoreLike) -> list[Asset]:
    if not contract.inputs:
        return []

    seen: set[str] = set()
    result: list[Asset] = []

    for input_name in contract.inputs:
        for asset in store.get_assets_by_name(input_name):
            if not asset.promptable:
                continue
                if asset.id not in seen:
                    seen.add(asset.id)
                    result.append(redact_for_disclosure(asset))

    return result
