"""Exact derivation proof for source-bound AI4S evidence Assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from aigineering.core.asset_versions import (
    SLICE_DERIVATION_VERSION,
    content_slice,
    create_replacement_claim,
    create_slice_asset,
)
from aigineering.core.disclosure import DisclosurePolicyError, compute_disclosure
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.verification import verify_replacement_claims
from aigineering.protocol.types import Asset, Contract


def _source() -> Asset:
    name = "retrieval_manifest"
    content = "W1\tGrounded title\nW2\tSecond title\n"
    return sign_asset(
        Asset(
            id=hash_asset_content(name, content),
            name=name,
            content=content,
            definition_hash=hash_asset_definition(name),
            content_hash=hash_asset_content(name, content),
            origin="tool",
            trust_tier="observed",
        )
    )


def _valid_slice(source: Asset):
    derived = create_slice_asset(
        source,
        slice_name="citations",
        range_spec="lines:1-1",
    )
    claim = create_replacement_claim(
        source.id,
        derived.id,
        definition_hash=derived.definition_hash,
        claim_type="slice",
        lineage_id=source.id,
        range_spec="lines:1-1",
    )
    return derived, claim


def test_slice_claim_recomputes_exact_source_range():
    store = MemoryStore()
    source = _source()
    derived, claim = _valid_slice(source)
    store.add_asset(source)
    store.add_asset(derived)

    result = verify_replacement_claims(store, [claim])

    assert result["pass_count"] == 1
    assert result["results"][0]["verification_level"] == "exact_derivation"
    assert claim.derivation_version == SLICE_DERIVATION_VERSION
    assert claim.range_spec == "lines:1-1"


def test_slice_constructor_rejects_caller_supplied_forgery():
    with pytest.raises(ValueError, match="does not match"):
        create_slice_asset(
            _source(),
            slice_name="citations",
            range_spec="lines:1-1",
            slice_content="W9\tFabricated title\n",
        )


def test_slice_verifier_rejects_forged_content_with_valid_lineage():
    store = MemoryStore()
    source = _source()
    forged_name = "citations"
    forged_content = "W9\tFabricated title\n"
    forged = sign_asset(
        Asset(
            id=hash_asset_content(forged_name, forged_content),
            name=forged_name,
            content=forged_content,
            definition_hash=hash_asset_definition(forged_name),
            content_hash=hash_asset_content(forged_name, forged_content),
            lineage_id=source.id,
            origin="worker",
        )
    )
    claim = create_replacement_claim(
        source.id,
        forged.id,
        definition_hash=forged.definition_hash,
        claim_type="slice",
        lineage_id=source.id,
        range_spec="lines:1-1",
    )
    store.add_asset(source)
    store.add_asset(forged)

    result = verify_replacement_claims(store, [claim])

    assert result["fail_count"] == 1
    assert any("does not equal" in issue for issue in result["results"][0]["issues"])


def test_sensitive_disclosure_requires_verified_incoming_slice_claim():
    store = MemoryStore()
    source = _source()
    derived, claim = _valid_slice(source)
    store.add_asset(source)
    store.add_asset(derived)
    contract = Contract(
        id="synthesis",
        inputs=("citations",),
        outputs=("research_report",),
        sensitive_input_policy={"accepted_claim_types": ["slice"]},
    )

    with pytest.raises(DisclosurePolicyError, match="no verified incoming claim"):
        compute_disclosure(contract, store)

    store.add_replacement_claim(claim)
    assert compute_disclosure(contract, store) == [derived]


def test_utf8_byte_ranges_are_exact_and_reject_split_characters():
    assert content_slice("A中B", "utf8-bytes:1-4") == "中"
    with pytest.raises(ValueError, match="splits a character"):
        content_slice("A中B", "utf8-bytes:1-3")
    with pytest.raises(ValueError, match="exceeds"):
        content_slice("A", "utf8-bytes:0-2")


def test_slice_derivation_reconstructs_after_sqlite_reopen(tmp_path: Path):
    path = str(tmp_path / "derivation.db")
    store = SQLiteStore(path)
    source = _source()
    derived, claim = _valid_slice(source)
    store.add_asset(source)
    store.add_asset(derived)
    store.add_replacement_claim(claim)
    store.close()

    reopened = SQLiteStore(path)
    restored = reopened.get_claims_for_replacement_asset(derived.id)

    assert restored == [claim]
    assert verify_replacement_claims(reopened, restored)["pass_count"] == 1
    assert reopened.schema_version == 17
    reopened.close()
