"""Exact input Asset selection for optional signed Contract bindings."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from aigineering.protocol.types import Asset, Contract


class InputAssetBindingIssue(Enum):
    MISSING = "input_asset_bindings references a missing Asset"
    WRONG_ROLE = "input_asset_bindings Asset name does not match its role"
    TOMBSTONED = "input_asset_bindings cannot reference a tombstoned Asset"


def input_asset_binding_issue(
    role: str, asset: Asset | None
) -> InputAssetBindingIssue | None:
    if asset is None:
        return InputAssetBindingIssue.MISSING
    if asset.name != role:
        return InputAssetBindingIssue.WRONG_ROLE
    if asset.tombstoned:
        return InputAssetBindingIssue.TOMBSTONED
    return None


def input_asset_bindings(policy: object) -> dict[str, tuple[str, ...]]:
    """Return validated `input_asset_bindings` from sensitive policy data."""
    if policy is None:
        return {}
    if not isinstance(policy, Mapping):
        raise ValueError("sensitive_input_policy must be an object")
    value = policy.get("input_asset_bindings", {})
    if not isinstance(value, Mapping):
        raise ValueError(
            "sensitive_input_policy.input_asset_bindings must be an object"
        )
    result: dict[str, tuple[str, ...]] = {}
    for role, asset_ids in value.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError("input_asset_bindings role names must be non-empty text")
        if not isinstance(asset_ids, (list, tuple)) or not all(
            isinstance(asset_id, str) and asset_id.strip() for asset_id in asset_ids
        ):
            raise ValueError(
                f"input_asset_bindings[{role!r}] must be non-empty Asset ID strings"
            )
        normalized = tuple(asset_ids)
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError(
                f"input_asset_bindings[{role!r}] must be sorted and unique"
            )
        result[role] = normalized
    return result


def validate_input_asset_binding_targets(
    contract: Contract, assets: tuple[Asset, ...] | list[Asset]
) -> None:
    """Require every declared binding to name a committed exact-role Asset."""
    bindings = input_asset_bindings(contract.sensitive_input_policy)
    if not bindings:
        return
    by_id = {asset.id: asset for asset in assets}
    for role, asset_ids in bindings.items():
        if role not in contract.inputs:
            continue
        for asset_id in asset_ids:
            issue = input_asset_binding_issue(role, by_id.get(asset_id))
            if issue is not None:
                raise ValueError(issue.value)


def read_contract_input_assets(contract: Contract, store: Any) -> list[Asset]:
    """Select raw input Assets, using exact bindings for mapped roles.

    Roles absent from the map retain their historical name lookup. A mapped
    role never falls back to name lookup when an exact binding is missing or
    invalid.
    """
    bindings = input_asset_bindings(contract.sensitive_input_policy)
    result: list[Asset] = []
    for role in contract.inputs:
        if role not in bindings:
            result.extend(store.get_assets_by_name(role))
            continue
        for asset_id in bindings[role]:
            asset = store.get_asset(asset_id)
            issue = input_asset_binding_issue(role, asset)
            if issue is not None:
                raise ValueError(issue.value)
            result.append(asset)
    return result


__all__ = [
    "InputAssetBindingIssue",
    "input_asset_binding_issue",
    "input_asset_bindings",
    "read_contract_input_assets",
    "validate_input_asset_binding_targets",
]
