"""Shared publication batch for a slice and its exact source relation."""

from aigineering.core.asset_versions import create_replacement_claim, create_slice_asset
from aigineering.core.verification import verify_replacement_claims
from aigineering.protocol.candidate import CandidateEffect
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    replacement_claim_effect,
)
from aigineering.protocol.types import Asset


def slice_publication_effects(
    store, source: Asset, *, slice_name: str, range_spec: str
) -> tuple[tuple[CandidateEffect, CandidateEffect], str]:
    """Compile and verify the pair before it enters Candidate commitment."""
    sliced = create_slice_asset(source, slice_name=slice_name, range_spec=range_spec)
    claim = create_replacement_claim(
        source_asset_id=source.id,
        replacement_asset_id=sliced.id,
        definition_hash=sliced.definition_hash,
        claim_type="slice",
        lineage_id=source.lineage_id or source.id,
        range_spec=range_spec,
    )

    class SliceInputs:
        def get_asset(self, asset_id):
            return sliced if asset_id == sliced.id else store.get_asset(asset_id)

        def __getattr__(self, name):
            return getattr(store, name)

    verification = verify_replacement_claims(SliceInputs(), [claim])
    if verification["fail_count"]:
        raise ValueError("; ".join(verification["results"][0]["issues"]))
    return (
        asset_proposal_effect(sliced),
        replacement_claim_effect(claim, replacement_effect_index=0),
    ), claim.id
