"""Tests for batch verification and sensitive input policy (v0.3.13)."""

from aigineering.core.ids import (
    hash_asset_content,
    hash_asset_definition,
)
from aigineering.core.store import MemoryStore
from aigineering.core.verification import (
    batch_verify_definition,
    check_sensitive_input_policy,
    verify_replacement_claims,
)
from aigineering.core.provenance import sign_asset
from aigineering.protocol.types import Asset, Contract, ReplacementClaim


def _make_asset(name: str, content: str, **kwargs: object) -> Asset:
    """Create a signed Asset with correct definition_hash and content_hash."""
    extra = dict(kwargs)
    if "origin" not in extra:
        extra["origin"] = "test"
    asset = Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content),
        **extra,  # type: ignore[arg-type]
    )
    return sign_asset(asset)


def _make_broken_asset(name: str, content: str) -> Asset:
    """Create a signed Asset with deliberately wrong content_hash (tampered)."""
    asset = Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        definition_hash=hash_asset_definition(name),
        content_hash="content:deadbeef00000000000000000000000000000000000000000000000000000000",
        origin="test",
    )
    return sign_asset(asset)


# ---------------------------------------------------------------------------
# batch_verify_definition
# ---------------------------------------------------------------------------


class TestBatchVerifyDefinition:
    def test_verify_content_hashes_under_definition(self):
        """batch_verify_definition returns results for every asset under a def_hash."""
        store = MemoryStore()
        name = "report"

        a1 = _make_asset(name, "v1")
        a2 = _make_asset(name, "v2")
        store.add_asset(a1)
        store.add_asset(a2)

        def_hash = hash_asset_definition(name)
        result = batch_verify_definition(store, def_hash)

        assert result["pass_count"] == 2
        assert result["fail_count"] == 0
        assert len(result["results"]) == 2

        ids = {r["asset_id"] for r in result["results"]}
        assert a1.id in ids
        assert a2.id in ids

        for r in result["results"]:
            assert r["valid"] is True

    def test_verify_all_pass(self):
        """When all content hashes are correct, pass_count == total_count."""
        store = MemoryStore()
        name = "dataset"

        for i in range(1, 6):
            store.add_asset(_make_asset(name, f"content-{i}"))

        def_hash = hash_asset_definition(name)
        result = batch_verify_definition(store, def_hash)

        assert result["pass_count"] == 5
        assert result["fail_count"] == 0
        assert all(r["valid"] for r in result["results"])

    def test_verify_detects_mismatch(self):
        """A tampered content_hash is flagged as invalid."""
        store = MemoryStore()
        name = "tampered_report"

        good = _make_asset(name, "legit content")
        bad = _make_broken_asset(name, "legit content - but hash is wrong")
        store.add_asset(good)
        store.add_asset(bad)

        def_hash = hash_asset_definition(name)
        result = batch_verify_definition(store, def_hash)

        assert result["pass_count"] == 1
        assert result["fail_count"] == 1

        for r in result["results"]:
            if r["asset_id"] == good.id:
                assert r["valid"] is True
            elif r["asset_id"] == bad.id:
                assert r["valid"] is False

    def test_empty_definition_returns_zero_counts(self):
        """When no assets match the definition, counts are zero."""
        store = MemoryStore()
        result = batch_verify_definition(store, "def:nonexistent")
        assert result["pass_count"] == 0
        assert result["fail_count"] == 0
        assert result["results"] == []


# ---------------------------------------------------------------------------
# check_sensitive_input_policy
# ---------------------------------------------------------------------------


class TestSensitiveInputPolicy:
    def test_sensitive_input_missing_signer_denied(self):
        """Policy with required_signer that has not signed any asset → not compliant."""
        store = MemoryStore()
        store.add_asset(
            _make_asset("input_data", "secret content", signed_by="other-signer")
        )

        contract = Contract(
            id="task:test123",
            name="sensitive_task",
            inputs=["input_data"],
            outputs=["report"],
            activation="input_data",
            sensitive_input_policy={
                "required_signer": "trusted-signer",
                "required_definition_hashes": [hash_asset_definition("input_data")],
            },
        )

        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is False
        assert any("required_signer" in v for v in result["violations"])

    def test_sensitive_input_hash_mismatch_denied(self):
        """Policy with required definition hash that has no assets → not compliant."""
        store = MemoryStore()
        # No assets at all
        contract = Contract(
            id="task:test456",
            name="sensitive_task",
            inputs=["missing_data"],
            outputs=["report"],
            activation="missing_data",
            sensitive_input_policy={
                "required_definition_hashes": [hash_asset_definition("missing_data")],
            },
        )

        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is False
        assert any("no assets in store" in v for v in result["violations"])

    def test_sensitive_input_all_ok_accepted(self):
        """Policy where all requirements are met → compliant."""
        store = MemoryStore()
        def_hash = hash_asset_definition("trusted_input")

        store.add_asset(
            _make_asset(
                "trusted_input",
                "safe content",
                signed_by="trusted-signer",
                trust_tier="verified",
            )
        )

        contract = Contract(
            id="task:test789",
            name="sensitive_task",
            inputs=["trusted_input"],
            outputs=["report"],
            activation="trusted_input",
            sensitive_input_policy={
                "required_signer": "trusted-signer",
                "required_definition_hashes": [def_hash],
                "required_trust_tier": "high",
                "accepted_claim_types": ["replacement"],
            },
        )

        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is True
        assert result["violations"] == []

    def test_sensitive_input_no_policy_returns_compliant(self):
        """When contract has no sensitive_input_policy, it's compliant by default."""
        store = MemoryStore()
        contract = Contract(
            id="task:no_policy",
            name="plain_task",
            outputs=["result"],
            activation="",
        )
        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is True
        assert result["violations"] == []

    def test_sensitive_input_invalid_claim_type_flagged(self):
        """accepted_claim_types with an invalid type → violation."""
        store = MemoryStore()
        contract = Contract(
            id="task:bad_type",
            name="sensitive_task",
            inputs=["data"],
            outputs=["report"],
            activation="data",
            sensitive_input_policy={
                "accepted_claim_types": ["banana", "replacement"],
            },
        )

        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is False
        assert any("banana" in v for v in result["violations"])

    def test_sensitive_input_trust_tier_too_low(self):
        """required_trust_tier not met by any asset → not compliant."""
        store = MemoryStore()
        store.add_asset(_make_asset("low_trust", "data", trust_tier="low"))

        contract = Contract(
            id="task:low_tier",
            name="sensitive_task",
            inputs=["low_trust"],
            outputs=["report"],
            activation="low_trust",
            sensitive_input_policy={
                "required_trust_tier": "high",
            },
        )

        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is False
        assert any("trust_tier" in v for v in result["violations"])

    def test_sensitive_input_policy_via_explicit_policy_param(self):
        """check_sensitive_input_policy accepts policy via explicit parameter."""
        store = MemoryStore()
        contract = Contract(
            id="task:plain",
            name="plain",
            outputs=["result"],
            activation="",
            # No sensitive_input_policy on the contract itself
        )

        policy = {
            "required_signer": "alice",
        }

        result = check_sensitive_input_policy(contract, store, policy=policy)
        assert result["compliant"] is False
        assert any("required_signer" in v for v in result["violations"])

    def test_sensitive_input_invalid_trust_tier_name_flagged(self):
        """An unrecognized trust tier name → violation."""
        store = MemoryStore()
        contract = Contract(
            id="task:bad_tier",
            name="sensitive_task",
            outputs=["report"],
            activation="",
            sensitive_input_policy={
                "required_trust_tier": "banana_tier",
            },
        )

        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is False
        assert any("not a recognized tier" in v for v in result["violations"])

    def test_sensitive_input_invalid_asset_tier_does_not_crash(self):
        """Asset with unrecognized trust_tier does not crash policy check."""
        from aigineering.core.ids import hash_asset_definition

        store = MemoryStore()
        store.add_asset(
            _make_asset("bad_asset", "content", trust_tier="banana_tier")
        )
        def_hash = hash_asset_definition("bad_asset")

        contract = Contract(
            id="task:invalid_asset_tier",
            name="bad_tier_task",
            inputs=["bad_asset"],
            outputs=["report"],
            activation="bad_asset",
            sensitive_input_policy={
                "required_trust_tier": "verified",
                "required_definition_hashes": [def_hash],
            },
        )
        # Must not raise ValueError; invalid tier → non-compliant.
        result = check_sensitive_input_policy(contract, store)
        assert result["compliant"] is False


# ---------------------------------------------------------------------------
# verify_replacement_claims
# ---------------------------------------------------------------------------


class TestVerifyReplacementClaims:
    def test_verify_claims_all_valid(self):
        """All claims valid when source and replacement assets exist and match."""
        store = MemoryStore()
        name = "shared_def"
        def_hash = hash_asset_definition(name)

        source = _make_asset(name, "original content")
        replacement = _make_asset(name, "replacement content")
        store.add_asset(source)
        store.add_asset(replacement)

        claim = ReplacementClaim(
            id="claim:test1",
            source_asset_id=source.id,
            replacement_asset_id=replacement.id,
            definition_hash=def_hash,
            claim_type="replacement",
        )

        result = verify_replacement_claims(store, [claim])
        assert result["pass_count"] == 1
        assert result["fail_count"] == 0
        assert result["results"][0]["valid"] is True

    def test_verify_claims_detects_missing_source(self):
        """Claim with missing source asset → fail."""
        store = MemoryStore()
        name = "shared_def"
        def_hash = hash_asset_definition(name)

        replacement = _make_asset(name, "content")
        store.add_asset(replacement)

        claim = ReplacementClaim(
            id="claim:test2",
            source_asset_id="asset:nonexistent",
            replacement_asset_id=replacement.id,
            definition_hash=def_hash,
            claim_type="replacement",
        )

        result = verify_replacement_claims(store, [claim])
        assert result["pass_count"] == 0
        assert result["fail_count"] == 1
        assert "not found" in result["results"][0]["issues"][0]

    def test_verify_claims_detects_def_mismatch(self):
        """Claim with definition_hash that doesn't match assets → fail."""
        store = MemoryStore()
        name = "shared_def"
        wrong_name = "other_def"

        source = _make_asset(name, "content")
        replacement = _make_asset(name, "other content")
        store.add_asset(source)
        store.add_asset(replacement)

        claim = ReplacementClaim(
            id="claim:test3",
            source_asset_id=source.id,
            replacement_asset_id=replacement.id,
            definition_hash=hash_asset_definition(wrong_name),  # wrong def hash
            claim_type="replacement",
        )

        result = verify_replacement_claims(store, [claim])
        assert result["fail_count"] == 1
        assert any("definition_hash" in iss for iss in result["results"][0]["issues"])
