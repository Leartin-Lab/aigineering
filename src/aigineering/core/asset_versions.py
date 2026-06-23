"""Asset versioning, slicing, and replacement claim helpers.

All operations are ADDITIVE — source assets are never mutated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.ids import hash_asset_content, hash_asset_definition, hash_claim
from aigineering.core.provenance import sign_asset

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, ReplacementClaim


def content_slice(content: str, range_spec: str) -> str:
    """Return the slice of *content* described by *range_spec*.

    Supported forms:
    - ``lines:start-end``: 1-based, inclusive line range.
    - ``chars:start-end``: 0-based, end-exclusive character range.

    Empty ``range_spec`` returns the original content. Invalid ranges fail
    closed with ``ValueError`` rather than silently producing the wrong asset.
    """
    if not range_spec:
        return content

    kind, sep, bounds = range_spec.partition(":")
    if sep != ":":
        raise ValueError(
            "range_spec must be empty or use 'lines:start-end' / 'chars:start-end'"
        )
    start_s, dash, end_s = bounds.partition("-")
    if dash != "-" or not start_s or not end_s:
        raise ValueError("range_spec bounds must use start-end")
    try:
        start = int(start_s)
        end = int(end_s)
    except ValueError as e:
        raise ValueError("range_spec bounds must be integers") from e
    if start < 0 or end < start:
        raise ValueError("range_spec bounds must be non-negative and ordered")

    if kind == "lines":
        if start < 1:
            raise ValueError("line ranges are 1-based")
        lines = content.splitlines(keepends=True)
        return "".join(lines[start - 1:end])
    if kind == "chars":
        return content[start:end]
    raise ValueError("range_spec kind must be 'lines' or 'chars'")


def create_slice_asset(
    source: Asset,
    *,
    slice_name: str,
    slice_content: str | None = None,
    range_spec: str = "",
) -> Asset:
    """Create a new asset that is a slice of *source*.

    The slice is a separate asset with its own hashes, linked to the
    source via lineage metadata.  The source asset is never modified.

    Returns a signed Asset.
    """
    from aigineering.protocol.types import Asset

    lineage_id = source.lineage_id or source.id
    if slice_content is None:
        slice_content = content_slice(source.content, range_spec)

    asset = Asset(
        id=hash_asset_content(slice_name, slice_content),
        name=slice_name,
        content=slice_content,
        origin=f"slice_of:{source.name}:{range_spec}" if range_spec else f"slice_of:{source.name}",
        trust_tier=source.trust_tier,
        source_uri=source.source_uri,
        lineage_id=lineage_id,
        definition_hash=hash_asset_definition(slice_name),
        content_hash=hash_asset_content(slice_name, slice_content),
    )
    return sign_asset(asset)


def create_replacement_claim(
    source_asset_id: str,
    replacement_asset_id: str,
    *,
    definition_hash: str = "",
    claim_type: str = "replacement",
    signed_by: str = "",
    provenance_seal: str = "",
) -> ReplacementClaim:
    """Create a replacement claim linking source to replacement.

    Parameters
    ----------
    claim_type : str
        Must be one of ``ReplacementClaim._VALID_CLAIM_TYPES``.
    """
    from aigineering.protocol.types import ReplacementClaim

    return ReplacementClaim(
        id=hash_claim(source_asset_id, replacement_asset_id, claim_type),
        source_asset_id=source_asset_id,
        replacement_asset_id=replacement_asset_id,
        definition_hash=definition_hash,
        claim_type=claim_type,
        signed_by=signed_by,
        provenance_seal=provenance_seal,
    )


def list_versions(store, name: str) -> list[Asset]:
    """Return all assets with *name*, sorted by content_hash.

    Returns an empty list if no matching assets exist.
    """
    matches = store.get_assets_by_name(name)
    return sorted(matches, key=lambda a: a.content_hash)


def resolve_latest(store, name: str) -> Asset | None:
    """Return the most recent asset with *name*.

    "Most recent" means highest content_hash (deterministic ordering).
    Returns None if no matching asset exists.
    """
    versions = list_versions(store, name)
    return versions[-1] if versions else None
