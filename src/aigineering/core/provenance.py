"""Asset provenance helpers."""

from __future__ import annotations

from dataclasses import replace

from aigineering.core.ids import hash_content
from aigineering.protocol.types import Asset
from aigineering.protocol.wire import asset_to_canonical


def default_signer(asset: Asset) -> str:
    """Return the default signer identity for an asset."""
    return asset.signed_by or asset.minted_by or asset.created_by or asset.origin


def provenance_signature(asset: Asset, signed_by: str | None = None) -> str:
    """Compute deterministic content-bound provenance signature.

    This is not a public-key cryptographic signature. It binds the canonical
    asset content and provenance metadata to a signer string for stable audit.
    """
    signer = signed_by or default_signer(asset)
    payload = f"{asset_to_canonical(replace(asset, signed_by=signer, signature=''))}|signed_by={signer}"
    return f"asig_{hash_content(payload)}"


def sign_asset(asset: Asset, signed_by: str | None = None) -> Asset:
    """Return a copy of *asset* with deterministic provenance signature fields."""
    signer = signed_by or default_signer(asset)
    signature = provenance_signature(asset, signed_by=signer)
    return replace(asset, signed_by=signer, signature=signature)
