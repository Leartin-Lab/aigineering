"""Asset provenance helpers."""

from __future__ import annotations

from dataclasses import replace

from aigineering.core.ids import compute_content_hash
from aigineering.protocol.types import Asset
from aigineering.protocol.wire import asset_to_canonical


def default_signer(asset: Asset) -> str:
    """Return the default signer identity for an asset."""
    return asset.signed_by or asset.minted_by or asset.created_by or asset.origin


def compute_provenance_seal(asset: Asset, signed_by: str | None = None) -> str:
    """Compute deterministic content-bound provenance seal.

    This is not a public-key cryptographic signature. It binds the canonical
    asset content and provenance metadata to a signer string for stable audit.
    """
    signer = signed_by or default_signer(asset)
    payload = f"{asset_to_canonical(replace(asset, signed_by=signer, provenance_seal=''))}|signed_by={signer}"
    return f"asig_{compute_content_hash(payload)}"


def sign_asset(asset: Asset, signed_by: str | None = None) -> Asset:
    """Return a copy of *asset* with deterministic provenance seal fields."""
    signer = signed_by or default_signer(asset)
    seal = compute_provenance_seal(asset, signed_by=signer)
    return replace(asset, signed_by=signer, provenance_seal=seal)


def verify_asset_seal(asset: Asset) -> bool:
    """Return whether *asset* has a valid deterministic provenance seal."""

    if not asset.signed_by or not asset.provenance_seal:
        return False
    return asset.provenance_seal == compute_provenance_seal(asset, signed_by=asset.signed_by)
