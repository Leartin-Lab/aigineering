"""Legacy review vectors retain signed-wire compatibility, not fresh writers."""

import json
from pathlib import Path

from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)


def test_legacy_review_vector_candidates_keep_canonical_signing_bytes():
    vectors = json.loads(
        Path("conformance/v0.5.13/task-review-vectors.json").read_text()
    )
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    for item in vectors["effect_vectors"]:
        candidate = candidate_proposal_from_dict(item["candidate"]["value"])
        verify_candidate_proposal(candidate, genesis)
        assert (
            candidate_signing_bytes(candidate).decode()
            == item["candidate"]["expected_signing_utf8"]
        )
