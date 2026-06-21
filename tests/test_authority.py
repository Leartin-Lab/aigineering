"""Tests for authority checker — the commitment boundary."""

import pytest

from aigineering.core.authority import check_authority, RESERVED_PREFIXES
from aigineering.protocol.types import Contract


def contract_with_outputs(*outputs: str) -> Contract:
    return Contract(id="c1", name="test", outputs=list(outputs))


def test_declared_output_accepted():
    acc, rej, _ = check_authority(
        contract_with_outputs("report"),
        [{"name": "report", "content": "hello"}],
    )
    assert len(acc) == 1
    assert len(rej) == 0
    assert acc[0]["name"] == "report"


def test_undeclared_output_rejected():
    acc, rej, _ = check_authority(
        contract_with_outputs("report"),
        [{"name": "citation", "content": "Smith 2025"}],
    )
    assert len(acc) == 0
    assert len(rej) == 1
    assert "not in contract.outputs" in rej[0]["reject_reason"]
    assert rej[0]["category"] == "authority_rejection"


def test_reserved_prefix_rejected():
    acc, rej, _ = check_authority(
        contract_with_outputs("_sys_config"),
        [{"name": "_sys_config", "content": "secret"}],
    )
    assert len(acc) == 0
    assert len(rej) == 1
    assert "reserved prefix" in rej[0]["reject_reason"]
    assert rej[0]["category"] == "protected_name_rejection"


def test_mixed_accept_reject():
    acc, rej, _ = check_authority(
        contract_with_outputs("report", "data"),
        [
            {"name": "report", "content": "r"},
            {"name": "citation", "content": "c"},  # undeclared
            {"name": "data", "content": "d"},
        ],
    )
    assert len(acc) == 2
    assert len(rej) == 1
    assert rej[0]["name"] == "citation"


def test_empty_outputs_allows_nothing():
    acc, rej, _ = check_authority(
        Contract(id="c2", name="empty", outputs=[]),
        [{"name": "anything", "content": "x"}],
    )
    assert len(acc) == 0
    assert len(rej) == 1


def test_rejected_has_category():
    acc, rej, _ = check_authority(
        Contract(id="c1", outputs=["x"]),
        [{"name": "y", "content": "z"}],
    )
    assert rej[0]["category"] == "authority_rejection"


def test_authority_policy_returned():
    acc, rej, policy = check_authority(
        Contract(id="c1", outputs=["report"]),
        [{"name": "report", "content": "hello"}],
    )
    assert "declared_outputs" in policy
    assert "reserved_prefixes" in policy
    assert policy["declared_outputs"] == ("report",)
    assert len(policy["reserved_prefixes"]) == len(RESERVED_PREFIXES)


@pytest.mark.parametrize("prefix", sorted(RESERVED_PREFIXES))
def test_every_reserved_prefix_rejected(prefix):
    name = f"{prefix}test_asset"
    contract = Contract(id="c1", outputs=[name])
    acc, rej, _ = check_authority(contract, [{"name": name, "content": "x"}])
    assert len(rej) == 1
    assert rej[0]["category"] == "protected_name_rejection"


def test_tool_obs_names_rejected():
    for name in ("_tool_obs_test", "_tool_call_test"):
        contract = Contract(id="c1", outputs=[name])
        acc, rej, _ = check_authority(contract, [{"name": name, "content": "x"}])
        assert len(rej) == 1
        assert rej[0]["category"] == "protected_name_rejection"


def test_system_contract_can_mint_declared_reserved_output_with_exact_authority():
    """System contract with exact minting_authority CAN mint protected names.

    After G5 gate fix: origin==system is NOT sufficient — exact
    minting_authority is required.
    """
    contract = Contract(
        id="system_contract",
        outputs=["_plan_result_parent"],
        origin="system",
        minting_authority=["_plan_result_parent"],
    )

    acc, rej, _ = check_authority(
        contract,
        [{"name": "_plan_result_parent", "content": "plan"}],
    )

    assert acc == [{"name": "_plan_result_parent", "content": "plan"}], (
        f"G5: With exact minting_authority, protected output should be accepted. "
        f"Got rejected: {rej}"
    )
    assert rej == []


class TestTrustPolicyPrefixProtection:
    """TrustPolicy without reserved_prefixes must not weaken the default gate."""

    def test_trust_policy_without_prefixes_still_blocks_reserved(self):
        """Passing TrustPolicy(minimum_trust_tier=...) without reserved_prefixes
        must still reject _sys_ and _mcp_ prefixes (fallback to RESERVED_PREFIXES)."""
        from aigineering.core.trust_policy import TrustPolicy
        from aigineering.protocol.types import TrustTier

        policy = TrustPolicy(minimum_trust_tier=TrustTier.OBSERVED)
        contract = Contract(
            id="task:prefix_regression",
            name="regression_test",
            outputs=("_sys_secret",),
            activation="",
            budget=5,
        )

        acc, rej, _ = check_authority(
            contract,
            [{"name": "_sys_secret", "content": "should be blocked"}],
            trust_policy=policy,
        )

        assert rej != [], (
            "P0 regression: TrustPolicy without reserved_prefixes allowed "
            "reserved prefix _sys_ — should fall back to RESERVED_PREFIXES"
        )
        assert acc == []

    def test_trust_policy_without_prefixes_still_blocks_mcp(self):
        """Same regression check for _mcp_ prefix."""
        from aigineering.core.trust_policy import TrustPolicy
        from aigineering.protocol.types import TrustTier

        policy = TrustPolicy(minimum_trust_tier=TrustTier.OBSERVED)
        contract = Contract(
            id="task:mcp_regression",
            name="mcp_regression",
            outputs=("_mcp_filesystem",),
            activation="",
            budget=5,
        )

        acc, rej, _ = check_authority(
            contract,
            [{"name": "_mcp_filesystem", "content": "should be blocked"}],
            trust_policy=policy,
        )

        assert rej != [], (
            "P0 regression: TrustPolicy without reserved_prefixes allowed "
            "reserved prefix _mcp_ — should fall back to RESERVED_PREFIXES"
        )
        assert acc == []
