"""Trusted behavior-label classification for disclosed prompt context."""

from __future__ import annotations

from aigineering.protocol.types import Asset, TrustTier


BEHAVIOR_LABEL_PREFIX = "behavior:"
MIN_BEHAVIOR_TRUST_TIER = TrustTier.CONFIGURED


def is_behavior_asset_allowed(asset: Asset) -> bool:
    """Return whether a behavior asset may be injected as worker instructions."""
    if not asset.name.startswith(BEHAVIOR_LABEL_PREFIX):
        return False
    try:
        tier = TrustTier.from_str(asset.trust_tier)
    except ValueError:
        return False
    return tier.value >= MIN_BEHAVIOR_TRUST_TIER.value
