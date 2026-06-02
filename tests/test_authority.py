"""Tests for authority checker — the commitment boundary."""

from aigineering.core.authority import check_authority, RESERVED_PREFIXES
from aigineering.protocol.types import Contract


def contract_with_outputs(*outputs: str) -> Contract:
    return Contract(id="c1", name="test", outputs=list(outputs))


def test_declared_output_accepted():
    acc, rej = check_authority(
        contract_with_outputs("report"),
        [{"name": "report", "content": "hello"}],
    )
    assert len(acc) == 1
    assert len(rej) == 0
    assert acc[0]["name"] == "report"


def test_undeclared_output_rejected():
    acc, rej = check_authority(
        contract_with_outputs("report"),
        [{"name": "citation", "content": "Smith 2025"}],
    )
    assert len(acc) == 0
    assert len(rej) == 1
    assert "not in contract.outputs" in rej[0]["reject_reason"]


def test_reserved_prefix_rejected():
    acc, rej = check_authority(
        contract_with_outputs("_sys_config"),
        [{"name": "_sys_config", "content": "secret"}],
    )
    assert len(acc) == 0
    assert len(rej) == 1
    assert "reserved prefix" in rej[0]["reject_reason"]


def test_mixed_accept_reject():
    acc, rej = check_authority(
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
    acc, rej = check_authority(
        Contract(id="c2", name="empty", outputs=[]),
        [{"name": "anything", "content": "x"}],
    )
    assert len(acc) == 0
    assert len(rej) == 1
