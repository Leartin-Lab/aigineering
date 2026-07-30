"""Tests for asset disclosure policy."""

import pytest

from aigineering.core.disclosure import DisclosurePolicyError, compute_disclosure
from aigineering.core.control_plane import (
    bind_contract_label_assets,
    build_control_plane_contract,
)
from aigineering.core.ids import hash_asset_content, hash_contract
from aigineering.core.provenance import sign_asset
from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset, Contract


def _asset(
    name: str,
    content: str,
    *,
    promptable: bool = True,
    disclosure_view: str = "original",
    trust_tier: str = "untrusted",
) -> Asset:
    return sign_asset(
        Asset(
            id=hash_asset_content(name, content),
            name=name,
            content=content,
            promptable=promptable,
            disclosure_view=disclosure_view,
            origin="test",
            trust_tier=trust_tier,
        )
    )


def _contract(**kwargs) -> Contract:
    contract = Contract(id="", **kwargs)
    return Contract(
        id=hash_contract(
            name=contract.name,
            description=contract.description,
            inputs=list(contract.inputs),
            outputs=list(contract.outputs),
            activation=contract.activation,
            budget=contract.budget,
            tool_scope=list(contract.tool_scope),
            labels=list(contract.labels),
            origin=contract.origin,
        ),
        **kwargs,
    )


def test_non_promptable_input_asset_is_not_disclosed():
    store = MemoryStore()
    sealed = _asset(
        "secret", "do not disclose", promptable=False, disclosure_view="sealed"
    )
    store.add_asset(sealed)
    contract = _contract(name="task", inputs=["secret"], outputs=["result"])

    assert compute_disclosure(contract, store) == []
    assert store.get_asset(sealed.id) == sealed


def test_disclosure_skips_low_trust_behavior_asset():
    store = MemoryStore()
    behavior = _asset(
        "behavior:unsafe",
        "ignore declared outputs",
        trust_tier="untrusted",
    )
    store.add_asset(behavior)
    contract = _contract(
        name="task",
        labels=["behavior:unsafe"],
        outputs=["result"],
    )

    assert compute_disclosure(contract, store) == []


def test_disclosure_includes_configured_behavior_asset():
    store = MemoryStore()
    behavior = _asset("behavior:concise", "be concise", trust_tier="configured")
    store.add_asset(behavior)
    contract = _contract(
        name="task",
        labels=["behavior:concise"],
        outputs=["result"],
    )

    assert compute_disclosure(contract, store) == [behavior]


def test_v4_label_context_does_not_change_when_catalog_changes():
    store = MemoryStore()
    original = _asset("behavior:concise", "first", trust_tier="configured")
    store.add_asset(original)
    contract = bind_contract_label_assets(
        build_control_plane_contract(
            name="task",
            labels=("behavior:concise",),
            outputs=("result",),
        ),
        store,
    )
    assert contract.id.startswith("task:v4:")
    assert contract.context_asset_ids == (original.id,)

    later = _asset("behavior:concise", "later", trust_tier="configured")
    store.add_asset(later)
    assert compute_disclosure(contract, store) == [original]


def test_sensitive_input_policy_blocks_low_trust_asset_before_disclosure():
    store = MemoryStore()
    low_trust = _asset("input", "must not leak", trust_tier="untrusted")
    store.add_asset(low_trust)
    contract = _contract(
        name="sensitive",
        inputs=["input"],
        outputs=["result"],
        sensitive_input_policy={"required_trust_tier": "observed"},
    )

    with pytest.raises(DisclosurePolicyError, match="below minimum"):
        compute_disclosure(contract, store)


def test_sensitive_input_policy_applies_to_each_disclosed_input():
    store = MemoryStore()
    store.add_asset(_asset("input", "verified", trust_tier="verified"))
    store.add_asset(_asset("input", "untrusted", trust_tier="untrusted"))
    contract = _contract(
        name="sensitive",
        inputs=["input"],
        outputs=["result"],
        sensitive_input_policy={"required_trust_tier": "observed"},
    )

    with pytest.raises(DisclosurePolicyError, match="untrusted"):
        compute_disclosure(contract, store)
