"""Tests for ReplacementClaim data model and lineage_id on Asset (v0.3.1d)."""

import json

import pytest

from aigineering.core.ids import hash_claim
from aigineering.core.provenance import sign_asset
from aigineering.core.store import JsonLStore, MemoryStore
from aigineering.protocol.types import Asset, ReplacementClaim
from aigineering.protocol.wire import asset_to_canonical, asset_to_dict


class TestReplacementClaimCreation:
    def test_claim_creation_and_validation(self):
        source_id = "asset:abc123"
        repl_id = "asset:def456"
        def_hash = "def:somehash"
        claim_id = hash_claim(source_id, repl_id, "replacement")
        lineage = "lineage:somehash"

        claim = ReplacementClaim(
            id=claim_id,
            source_asset_id=source_id,
            replacement_asset_id=repl_id,
            definition_hash=def_hash,
            claim_type="replacement",
            signed_by="signer-1",
            provenance_seal="sig-abc",
            lineage_id=lineage,
        )

        assert claim.id.startswith("claim:")
        assert claim.source_asset_id == source_id
        assert claim.replacement_asset_id == repl_id
        assert claim.definition_hash == def_hash
        assert claim.claim_type == "replacement"
        assert claim.signed_by == "signer-1"
        assert claim.provenance_seal == "sig-abc"
        assert claim.lineage_id == lineage

    def test_claim_types(self):
        valid_types = [
            "replacement",
            "slice",
            "summary",
            "redaction",
            "equivalent_input",
        ]
        for ct in valid_types:
            claim = ReplacementClaim(
                id=hash_claim("src", "repl", ct),
                source_asset_id="src",
                replacement_asset_id="repl",
                definition_hash="def:h",
                claim_type=ct,
            )
            assert claim.claim_type == ct

    def test_claim_requires_valid_ids(self):
        with pytest.raises(TypeError):
            ReplacementClaim(  # type: ignore[call-arg]
                id="claim:xxx",
                # missing source_asset_id
                replacement_asset_id="repl",
                definition_hash="def:h",
                claim_type="replacement",
            )

    def test_claim_defaults(self):
        claim = ReplacementClaim(
            id=hash_claim("src", "repl", "replacement"),
            source_asset_id="src",
            replacement_asset_id="repl",
            definition_hash="def:h",
            claim_type="replacement",
        )
        assert claim.signed_by == ""
        assert claim.provenance_seal == ""
        assert claim.lineage_id == ""

    def test_claim_is_frozen(self):
        claim = ReplacementClaim(
            id=hash_claim("src", "repl", "replacement"),
            source_asset_id="src",
            replacement_asset_id="repl",
            definition_hash="def:h",
            claim_type="replacement",
        )
        with pytest.raises(Exception):
            claim.claim_type = "slice"  # type: ignore[misc]


class TestLineageId:
    def test_lineage_id_defaults_to_empty(self):
        asset = Asset(id="a1", name="test", content="hello")
        assert asset.lineage_id == ""

    def test_lineage_id_persists_memory_store(self):
        asset = sign_asset(
            Asset(
                id="a1",
                name="test",
                content="hello",
                lineage_id="lineage:abc",
                origin="test",
            )
        )
        store = MemoryStore()
        store.add_asset(asset)
        retrieved = store.get_asset("a1")
        assert retrieved is not None
        assert retrieved.lineage_id == "lineage:abc"

    def test_lineage_id_persists_jsonl_store(self, tmp_path):
        assets_path = tmp_path / "assets.jsonl"
        contracts_path = tmp_path / "contracts.jsonl"

        asset = sign_asset(
            Asset(
                id="a1",
                name="test",
                content="hello",
                lineage_id="lineage:abc",
                origin="test",
            )
        )
        store = JsonLStore(str(assets_path), str(contracts_path))
        store.add_asset(asset)
        retrieved = store.get_asset("a1")
        assert retrieved is not None
        assert retrieved.lineage_id == "lineage:abc"

        # Re-load from disk and verify persistence
        store2 = JsonLStore(str(assets_path), str(contracts_path))
        retrieved2 = store2.get_asset("a1")
        assert retrieved2 is not None
        assert retrieved2.lineage_id == "lineage:abc"

    def test_lineage_id_not_in_canonical(self):
        """lineage_id is metadata, not identity — must NOT appear in canonical form."""
        asset = Asset(id="a1", name="test", content="hello", lineage_id="lineage:abc")
        canonical = asset_to_canonical(asset)
        parsed = json.loads(canonical)
        assert "lineage_id" not in parsed
        assert "name" in parsed
        assert "content" in parsed

    def test_lineage_id_in_dict_roundtrip(self):
        """lineage_id must be serialized/deserialized via dict representation."""
        asset = Asset(id="a1", name="test", content="hello", lineage_id="lineage:abc")
        d = asset_to_dict(asset)
        assert d["lineage_id"] == "lineage:abc"
        assert d["id"] == "a1"
        assert d["name"] == "test"

    def test_lineage_id_empty_in_canonical(self):
        """Even when lineage_id is set, canonical form excludes it."""
        asset = Asset(id="a1", name="test", content="hello", lineage_id="lineage:xyz")
        canonical = asset_to_canonical(asset)
        assert '"lineage_id"' not in canonical
        # Verify canonical includes identity fields
        assert '"name"' in canonical
        assert '"content"' in canonical

    def test_invalid_claim_type_raises_value_error(self) -> None:
        """claim_type='banana' raises ValueError."""
        with pytest.raises(ValueError, match="Invalid claim_type"):
            ReplacementClaim(
                id=hash_claim("src", "rep", "banana"),
                source_asset_id="src",
                replacement_asset_id="rep",
                definition_hash="def:test",
                claim_type="banana",
            )
