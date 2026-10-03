"""Optional helpers for ordered Asset reads without widening Store protocols."""

from __future__ import annotations

from collections.abc import Sequence

from aigineering.core.store_capabilities import AssetReader
from aigineering.protocol.types import Asset


def read_assets_by_ids(
    store: AssetReader, asset_ids: Sequence[str]
) -> list[Asset | None]:
    """Read Assets in requested order, using an optional batch adapter method.

    Missing IDs produce ``None`` and duplicate IDs remain duplicated in the
    result. Adapter results that were not requested are ignored.
    """
    ids = tuple(asset_ids)
    if not ids:
        return []

    batch_reader = getattr(store, "get_assets_by_ids", None)
    if callable(batch_reader):
        requested = set(ids)
        by_id = {
            asset.id: asset
            for asset in batch_reader(tuple(dict.fromkeys(ids)))
            if asset.id in requested
        }
        return [by_id.get(asset_id) for asset_id in ids]

    return [store.get_asset(asset_id) for asset_id in ids]
