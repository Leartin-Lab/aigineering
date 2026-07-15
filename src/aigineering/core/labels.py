"""Label resolution for declarative asset injection."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.protocol.types import Asset, Contract, TrustTier

if TYPE_CHECKING:
    from aigineering.core.runtime_ingress import RuntimeIngress

_logger = logging.getLogger(__name__)

BEHAVIOR_LABEL_PREFIX = "behavior:"
MIN_BEHAVIOR_TRUST_TIER = TrustTier.CONFIGURED

LABEL_MODE_RELEASE = "release"  # fail closed — no placeholders
LABEL_MODE_DEBUG = "debug"  # create diagnostic placeholders (current behavior)


class StoreLike(Protocol):
    def add_asset(self, asset: Asset) -> None: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...


@dataclass(frozen=True)
class Label:
    """Declarative rule that injects asset references into a contract context."""

    name: str
    assets: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "assets", tuple(self.assets))


@dataclass(frozen=True)
class LabelResolution:
    """Result of resolving labels against the current asset store."""

    label_names: tuple[str, ...]
    injected_assets: tuple[Asset, ...]
    placeholder_assets: tuple[Asset, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "label_names", tuple(self.label_names))
        object.__setattr__(self, "injected_assets", tuple(self.injected_assets))
        object.__setattr__(self, "placeholder_assets", tuple(self.placeholder_assets))


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
    ingress: RuntimeIngress,
    mode: str = LABEL_MODE_DEBUG,
) -> LabelResolution:
    """Resolve contract labels into context assets.

    Labels do not execute work and do not grant authority. They only inject
    asset references into the contract-local context.

    Missing dependencies are handled per *mode*:

    - ``"debug"`` (default): create diagnostic placeholder assets so the
      runtime can trace the gap (current behavior).
    - ``"release"``: fail closed — emit a warning and skip the dependency;
      no placeholder is created.
    """
    injected: list[Asset] = []
    placeholders: list[Asset] = []
    seen_ids: set[str] = set()

    def _persist(asset: Asset) -> Asset:
        """Persist a placeholder asset through ingress."""
        return ingress.accept_asset(
            asset, source="label_resolver", allow_protected=True
        )

    def _warn_or_placeholder(label_name: str, asset_name: str) -> Asset | None:
        """In release mode, emit a warning and return None (skip).
        In debug mode, create and persist a placeholder asset."""
        if mode == LABEL_MODE_RELEASE:
            _logger.warning(
                "Label '%s' dependency '%s' not present in asset store "
                "(label_mode=%s — skipping)",
                label_name,
                asset_name,
                mode,
            )
            return None
        return _persist(_placeholder_asset(label_name, asset_name))

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
                placeholder = _warn_or_placeholder(label_name, label_name)
                if placeholder is not None and placeholder.id not in seen_ids:
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
            missing_name = f"_label_missing_{label_name}"
            placeholder = _warn_or_placeholder(label_name, missing_name)
            if placeholder is not None and placeholder.id not in seen_ids:
                placeholders.append(placeholder)
                injected.append(placeholder)
                seen_ids.add(placeholder.id)
            continue

        for asset_name in label.assets:
            matches = store.get_assets_by_name(asset_name)
            if not matches:
                placeholder = _warn_or_placeholder(label.name, asset_name)
                if placeholder is None:
                    continue
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
