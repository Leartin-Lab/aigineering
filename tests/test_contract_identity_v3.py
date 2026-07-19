"""Security-complete v3 Contract identity regression tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.core.ids import (
    CONTRACT_SELF_REFERENCE,
    contract_identity_v3,
    hash_contract_v3,
)
from aigineering.core.methods import method_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.actions import parse_action
from aigineering.protocol.types import Contract


def _identity(**overrides) -> str:
    fields = {
        "name": "task",
        "description": "do work",
        "inputs": ("input",),
        "outputs": ("output",),
        "activation": "input",
        "budget": 1,
        "tool_scope": (),
        "labels": (),
        "origin": "system",
        "parent_id": "parent",
        "worker_capabilities": ("text",),
        "worker_pools": ("advanced",),
        "minting_authority": (f"_sys_{CONTRACT_SELF_REFERENCE}",),
        "sensitive_input_policy": {"required_trust_tier": "human"},
        "acceptance_policy": {"mode": "independent"},
    }
    fields.update(overrides)
    return hash_contract_v3(**fields)


def test_v3_identity_binds_authority_policy_and_routing_fields():
    reference = _identity()

    assert _identity(minting_authority=("_sys_other",)) != reference
    assert _identity(sensitive_input_policy=None) != reference
    assert _identity(acceptance_policy=None) != reference
    assert _identity(worker_capabilities=("vision",)) != reference
    assert _identity(worker_pools=("default",)) != reference
    assert _identity(parent_id="other-parent") != reference


def test_method_contract_normalizes_self_authority_and_inherits_security():
    parent = Contract(
        id="task:parent",
        name="parent",
        outputs=("report",),
        tool_scope=("lookup",),
        worker_capabilities=("tool",),
        worker_pools=("trusted",),
        sensitive_input_policy={"required_trust_tier": "configured"},
    )
    child = method_contract(
        parent,
        parse_action('/tool {"name": "lookup", "args": {}}'),
    )

    assert child.id.startswith("task:v3:")
    assert contract_identity_v3(child) == child.id
    assert child.worker_capabilities == (*parent.worker_capabilities, "tool-execution")
    assert child.worker_pools == parent.worker_pools
    assert child.sensitive_input_policy == parent.sensitive_input_policy
    assert "_tool_capability_lookup" in child.inputs


def test_v3_contract_tampering_fails_before_persistence():
    parent = Contract(id="task:parent", outputs=("report",))
    child = method_contract(parent, parse_action('/plan {"reason": "split"}'))
    tampered = replace(
        child,
        minting_authority=(*child.minting_authority, "_sys_escalated"),
    )
    store = SQLiteStore(":memory:")

    with pytest.raises(ValueError, match="canonical v3 identity"):
        store.add_contract(tampered)
    assert store.get_contract(child.id) is None
    store.close()
