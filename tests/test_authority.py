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


def test_system_contract_can_mint_declared_reserved_output():
    contract = Contract(
        id="system_contract",
        outputs=["_plan_result_parent"],
        origin="system",
    )

    acc, rej, _ = check_authority(
        contract,
        [{"name": "_plan_result_parent", "content": "plan"}],
    )

    assert acc == [{"name": "_plan_result_parent", "content": "plan"}]
    assert rej == []
