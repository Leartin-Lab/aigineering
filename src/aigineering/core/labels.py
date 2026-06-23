"""Label resolution for declarative asset injection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.provenance import sign_asset
from aigineering.protocol.types import Asset, Contract, TrustTier

BEHAVIOR_LABEL_PREFIX = "behavior:"
MIN_BEHAVIOR_TRUST_TIER = TrustTier.CONFIGURED


class StoreLike(Protocol):
    def add_asset(self, asset: Asset) -> None: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...


@dataclass(frozen=True)
class Label:
    """Declarative rule that injects asset references into a contract context."""

    name: str
    assets: list[str] = field(default_factory=list)
    description: str = ""


@dataclass(frozen=True)
class LabelResolution:
    """Result of resolving labels against the current asset store."""

    label_names: list[str]
    injected_assets: list[Asset]
    placeholder_assets: list[Asset]


def _placeholder_asset(label_name: str, asset_name: str) -> Asset:
    content = json.dumps(
        {
            "placeholder": True,
            "label": label_name,
            "asset": asset_name,
            "reason": "label dependency was not present in the asset store",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return Asset(
        id=hash_asset_content(asset_name, content),
        name=asset_name,
        content=content,
        definition_hash=hash_asset_definition(asset_name),
        content_hash=hash_asset_content(asset_name, content),
        content_type="application/x-aig-placeholder",
        origin="label_placeholder",
        trust_tier="untrusted",
        minted_by="label_resolver",
        promptable=False,
    )


def is_behavior_asset_allowed(asset: Asset) -> bool:
    """Return whether a behavior asset may be injected as worker instructions."""

    if not asset.name.startswith(BEHAVIOR_LABEL_PREFIX):
        return False
    try:
        tier = TrustTier.from_str(asset.trust_tier)
    except ValueError:
        return False
    return tier.value >= MIN_BEHAVIOR_TRUST_TIER.value


def resolve_contract_labels(
    contract: Contract,
    labels: dict[str, Label],
    store: StoreLike,
) -> LabelResolution:
    """Resolve contract labels into context assets.

    Labels do not execute work and do not grant authority. They only inject
    asset references into the contract-local context. Missing dependencies are
    represented as placeholder assets so the runtime can trace the gap.
    """
    injected: list[Asset] = []
    placeholders: list[Asset] = []
    seen_ids: set[str] = set()

    for label_name in contract.labels:
        # Behavior labels (behavior:*) are self-referencing — the label
        # name IS the asset name.  Resolve by looking up the asset
        # directly instead of requiring a registered Label object.
        if label_name.startswith(BEHAVIOR_LABEL_PREFIX):
            matches = store.get_assets_by_name(label_name)
            allowed_matches = [
                asset for asset in matches if is_behavior_asset_allowed(asset)
            ]
            if not allowed_matches:
                placeholder = sign_asset(_placeholder_asset(label_name, label_name))
                store.add_asset(placeholder)
                if placeholder.id not in seen_ids:
                    placeholders.append(placeholder)
                    injected.append(placeholder)
                    seen_ids.add(placeholder.id)
                continue
            for asset in allowed_matches:
                if asset.id not in seen_ids:
                    injected.append(asset)
                    seen_ids.add(asset.id)
            continue

        label = labels.get(label_name)
        if label is None:
            placeholder = sign_asset(
                _placeholder_asset(label_name, f"_label_missing_{label_name}")
            )
            store.add_asset(placeholder)
            if placeholder.id not in seen_ids:
                placeholders.append(placeholder)
                injected.append(placeholder)
                seen_ids.add(placeholder.id)
            continue

        for asset_name in label.assets:
            matches = store.get_assets_by_name(asset_name)
            if not matches:
                placeholder = sign_asset(_placeholder_asset(label.name, asset_name))
                store.add_asset(placeholder)
                matches = [placeholder]
                placeholders.append(placeholder)
            for asset in matches:
                if asset.id not in seen_ids:
                    injected.append(asset)
                    seen_ids.add(asset.id)

    return LabelResolution(
        label_names=list(contract.labels),
        injected_assets=injected,
        placeholder_assets=placeholders,
    )
