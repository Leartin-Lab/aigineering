"""Tests for the unified TrustPolicy engine."""
import pytest
from aigineering.core.trust_policy import TrustDecision, TrustPolicy
from aigineering.protocol.types import Asset, TrustTier


def _asset(name="test_asset", trust_tier="untrusted", signed_by="",
           origin="", labels=None):
    """Helper to create a minimal Asset for trust policy tests."""
    from aigineering.core.ids import hash_asset_definition, hash_asset_content
    content = f"content of {name}"
    def_hash = hash_asset_definition(name)
    cont_hash = hash_asset_content(name, content)
    return Asset(
        id=f"asset:{name}",
        name=name,
        content=content,
        definition_hash=def_hash,
        content_hash=cont_hash,
        trust_tier=trust_tier,
        signed_by=signed_by,
        origin=origin,
    )


class TestTrustDecision:
    def test_accept_has_empty_reasons(self):
        d = TrustDecision.accept()
        assert d.accepted is True
        assert d.reasons == frozenset()

    def test_reject_has_reasons(self):
        d = TrustDecision.reject("reason 1", "reason 2")
        assert d.accepted is False
        assert d.reasons == frozenset(["reason 1", "reason 2"])

    def test_immutable(self):
        d = TrustDecision(accepted=True, reasons=frozenset(["x"]))
        with pytest.raises(Exception):
            d.accepted = False


class TestTrustPolicyEvaluate:
    def test_empty_policy_accepts_everything(self):
        policy = TrustPolicy()
        asset = _asset()
        result = policy.evaluate([asset])
        assert result.accepted is True

    def test_minimum_trust_tier_accepts_above(self):
        policy = TrustPolicy(minimum_trust_tier=TrustTier.CONFIGURED)
        asset = _asset(trust_tier="verified")
        result = policy.evaluate([asset])
        assert result.accepted is True

    def test_minimum_trust_tier_rejects_below(self):
        policy = TrustPolicy(minimum_trust_tier=TrustTier.CONFIGURED)
        asset = _asset(trust_tier="observed")
        result = policy.evaluate([asset])
        assert result.accepted is False
        assert any("trust_tier" in r for r in result.reasons)

    def test_allowed_signers_accepts_known(self):
        policy = TrustPolicy(allowed_signers=frozenset(["alice"]))
        asset = _asset(signed_by="alice")
        result = policy.evaluate([asset])
        assert result.accepted is True

    def test_allowed_signers_rejects_unknown(self):
        policy = TrustPolicy(allowed_signers=frozenset(["alice"]))
        asset = _asset(signed_by="bob")
        result = policy.evaluate([asset])
        assert result.accepted is False
        assert any("signed_by" in r for r in result.reasons)

    def test_reserved_prefix_rejects_match(self):
        policy = TrustPolicy(reserved_prefixes=frozenset(["_sys_"]))
        asset = _asset(name="_sys_secret")
        result = policy.evaluate([asset])
        assert result.accepted is False
        assert any("reserved prefix" in r for r in result.reasons)

    def test_multiple_violations_accumulated(self):
        policy = TrustPolicy(
            minimum_trust_tier=TrustTier.CONFIGURED,
            allowed_signers=frozenset(["alice"]),
        )
        asset = _asset(trust_tier="untrusted", signed_by="bob")
        result = policy.evaluate([asset])
        assert result.accepted is False
        assert len(result.reasons) >= 2

    def test_unknown_trust_tier_rejected(self):
        policy = TrustPolicy(minimum_trust_tier=TrustTier.CONFIGURED)
        asset = _asset(trust_tier="banana_tier")
        result = policy.evaluate([asset])
        assert result.accepted is False
        assert any("unrecognized trust_tier" in r for r in result.reasons)


class TestTrustPolicyFromConfig:
    def test_full_config(self):
        policy = TrustPolicy.from_config({
            "minimum_trust_tier": "verified",
            "allowed_signers": ["alice"],
            "reserved_prefixes": ["_sys_"],
        })
        assert policy.minimum_trust_tier == TrustTier.VERIFIED
        assert policy.allowed_signers == frozenset(["alice"])
        assert policy.reserved_prefixes == frozenset(["_sys_"])
        assert policy.allowed_origins is None

    def test_partial_config_others_none(self):
        policy = TrustPolicy.from_config({"minimum_trust_tier": "observed"})
        assert policy.minimum_trust_tier == TrustTier.OBSERVED
        assert policy.allowed_signers is None

    def test_empty_config_permissive(self):
        policy = TrustPolicy.from_config({})
        assert policy.minimum_trust_tier is None
        result = policy.evaluate([_asset()])
        assert result.accepted is True

    def test_invalid_tier_raises(self):
        with pytest.raises(ValueError):
            TrustPolicy.from_config({"minimum_trust_tier": "banana_tier"})
