"""Tests for WorkerPackage serialization and validation."""

import json

import pytest

from aigineering.protocol.package import WorkerPackage


def test_worker_package_round_trip_basic():
    """WorkerPackage survives to_json → from_json with all fields populated."""
    wp = WorkerPackage(
        contract_id="c1",
        contract={"name": "root", "budget": 10, "outputs": ["report"]},
        disclosed_assets=[{"name": "input1", "content": "hello"}],
        method_context_assets=[{"name": "_plan_c1", "content": "plan result"}],
        tool_scope=["lookup", "search"],
        budget_remaining=5,
    )

    json_str = wp.to_json()
    restored = WorkerPackage.from_json(json_str)

    assert restored == wp
    assert restored.contract_id == "c1"
    assert restored.contract["name"] == "root"
    assert restored.contract["budget"] == 10
    assert restored.contract["outputs"] == ("report",)
    assert restored.budget_remaining == 5
    assert restored.tool_scope == ("lookup", "search")
    assert isinstance(restored.disclosed_assets, tuple)
    assert isinstance(restored.method_context_assets, tuple)
    assert isinstance(restored.tool_scope, tuple)


def test_worker_package_round_trip_with_capability_requirements():
    """WorkerPackage round-trip preserves capability_requirements."""
    wp = WorkerPackage(
        contract_id="c99",
        contract={"name": "advanced"},
        disclosed_assets=[],
        method_context_assets=[],
        tool_scope=[],
        budget_remaining=0,
        capability_requirements=["streaming", "vision"],
    )

    json_str = wp.to_json()
    restored = WorkerPackage.from_json(json_str)

    assert restored == wp
    assert restored.capability_requirements == ("streaming", "vision")


def test_worker_package_round_trip_preserves_claim_epoch():
    package = WorkerPackage(
        contract_id="c1",
        contract={},
        disclosed_assets=[],
        method_context_assets=[],
        tool_scope=[],
        budget_remaining=1,
        claim_id="lease:1",
        claim_epoch=3,
    )
    assert WorkerPackage.from_json(package.to_json()).claim_epoch == 3


def test_worker_package_capability_requirements_defaults_to_empty():
    """capability_requirements defaults to empty tuple when omitted."""
    wp = WorkerPackage(
        contract_id="c1",
        contract={"name": "t"},
        disclosed_assets=[],
        method_context_assets=[],
        tool_scope=[],
        budget_remaining=0,
    )

    assert wp.capability_requirements == ()
    json_str = wp.to_json()
    restored = WorkerPackage.from_json(json_str)
    assert restored.capability_requirements == ()


def test_worker_package_from_json_missing_required_field_raises():
    """Missing required fields fail with a stable protocol error."""
    with pytest.raises(ValueError, match="missing required field 'contract'"):
        WorkerPackage.from_json('{"contract_id": "c1"}')  # missing contract

    with pytest.raises(ValueError, match="missing required field 'contract_id'"):
        WorkerPackage.from_json('{"contract": {"name": "t"}}')  # missing contract_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract", []),
        ("disclosed_assets", {}),
        ("tool_scope", [1]),
        ("budget_remaining", True),
        ("claim_epoch", "1"),
    ],
)
def test_worker_package_from_json_rejects_invalid_field_types(field, value):
    package = WorkerPackage(
        contract_id="c1",
        contract={},
        disclosed_assets=[],
        method_context_assets=[],
        tool_scope=[],
        budget_remaining=1,
    )
    payload = json.loads(package.to_json())
    payload[field] = value
    with pytest.raises(ValueError, match=field):
        WorkerPackage.from_json(json.dumps(payload))


def test_worker_package_multi_worker_compatibility():
    """Same WorkerPackage shape works across different contract and asset types."""
    # Simulate a mock worker scenario: small contract, few assets
    mock_pkg = WorkerPackage(
        contract_id="mock_c1",
        contract={"name": "demo", "budget": 3, "outputs": ["result"]},
        disclosed_assets=[{"name": "config", "content": "{}"}],
        method_context_assets=[],
        tool_scope=[],
        budget_remaining=3,
    )

    # Simulate an LLM worker scenario: richer contract, tool scope, method context
    llm_pkg = WorkerPackage(
        contract_id="llm_c2",
        contract={
            "name": "analysis",
            "description": "Analyze data with tools",
            "budget": 10,
            "inputs": ["dataset"],
            "outputs": ["report"],
            "tool_scope": ["lookup", "search"],
        },
        disclosed_assets=[
            {"name": "dataset", "content": "some data"},
            {"name": "reference", "content": "background info"},
        ],
        method_context_assets=[{"name": "_plan_llm_c2", "content": "plan output"}],
        tool_scope=["lookup", "search"],
        budget_remaining=8,
        capability_requirements=["streaming"],
    )

    # Both packages serialize and deserialize correctly
    for pkg in (mock_pkg, llm_pkg):
        json_str = pkg.to_json()
        restored = WorkerPackage.from_json(json_str)
        assert restored == pkg
        assert isinstance(restored.disclosed_assets, tuple)
        assert isinstance(restored.method_context_assets, tuple)
        assert isinstance(restored.tool_scope, tuple)
        assert isinstance(restored.capability_requirements, tuple)


def test_worker_package_empty_collections_remain_empty():
    """Empty tuples remain empty through round-trip."""
    wp = WorkerPackage(
        contract_id="c1",
        contract={},
        disclosed_assets=[],
        method_context_assets=[],
        tool_scope=[],
        budget_remaining=0,
    )

    json_str = wp.to_json()
    restored = WorkerPackage.from_json(json_str)

    assert restored.disclosed_assets == ()
    assert restored.method_context_assets == ()
    assert restored.tool_scope == ()
    assert restored.capability_requirements == ()
