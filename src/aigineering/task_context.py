"""Read-only application views of the context selected for a task."""

from __future__ import annotations

from aigineering.core.asset_reads import read_assets_by_ids
from aigineering.core.disclosure import (
    REDACTED_CONTENT,
    DisclosurePolicyError,
    compute_disclosure,
    redact_for_disclosure,
)
from aigineering.core.store import require_operational_store
from aigineering.protocol.types import Asset, Contract


class TaskContextError(ValueError):
    """A safe, stable task-context inspection error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def method_context_assets_for(contract: Contract, store) -> tuple[Asset, ...]:
    """Return the method continuation context used by the Worker package."""
    store = require_operational_store(store)
    assets: list[Asset] = []
    seen: set[str] = set()
    asset_ids: list[str] = []
    for entry in store.get_by_relation_target(contract.id):
        if entry.event_type not in {
            "method_continuation_context_prepared",
            "method_continuation_scheduled",
        }:
            continue
        asset_ids.extend(entry.disclosed_assets)
    for asset in read_assets_by_ids(store, asset_ids):
        if asset is None or asset.id in seen:
            continue
        assets.append(redact_for_disclosure(asset))
        seen.add(asset.id)
    return tuple(assets)


def task_context(
    store,
    contract_id: str,
    *,
    asset_id: str | None = None,
    include_content: bool = False,
) -> dict:
    """Inspect current Worker context without publishing or changing facts."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise TaskContextError("invalid_arguments")
    if asset_id is not None and (not isinstance(asset_id, str) or not asset_id.strip()):
        raise TaskContextError("invalid_arguments")
    if not isinstance(include_content, bool):
        raise TaskContextError("invalid_arguments")
    if include_content and asset_id is None:
        raise TaskContextError("invalid_arguments")

    operational = require_operational_store(store)
    contract = operational.get_contract(contract_id)
    if contract is None:
        raise TaskContextError("task_not_found")
    try:
        disclosed = tuple(compute_disclosure(contract, operational))
    except DisclosurePolicyError as error:
        raise TaskContextError("disclosure_rejected") from error
    except (LookupError, TypeError, ValueError) as error:
        raise TaskContextError("context_invalid") from error
    method_context = method_context_assets_for(contract, operational)

    def metadata(asset: Asset) -> dict:
        return {
            "id": asset.id,
            "name": asset.name,
            "content_type": asset.content_type,
            "origin": asset.origin,
            "trust_tier": asset.trust_tier,
            "promptable": asset.promptable,
            "disclosure_view": asset.disclosure_view,
            "tombstoned": asset.tombstoned,
        }

    result = {
        "ok": True,
        "data": {
            "contract_id": contract.id,
            "disclosed_assets": [metadata(asset) for asset in disclosed],
            "method_context_assets": [metadata(asset) for asset in method_context],
        },
        "revision": operational.get_runtime_revision(),
    }
    if asset_id is None:
        return result
    selected = next(
        (asset for asset in (*disclosed, *method_context) if asset.id == asset_id),
        None,
    )
    if selected is None:
        raise TaskContextError("asset_not_disclosed")
    selected_data = metadata(selected)
    if include_content:
        if not selected.promptable or selected.tombstoned:
            raise TaskContextError("asset_content_forbidden")
        content = selected.content
        if selected.disclosure_view != "original" or content == REDACTED_CONTENT:
            content = REDACTED_CONTENT
        selected_data["content"] = content
    result["data"]["selected_asset"] = selected_data
    return result
