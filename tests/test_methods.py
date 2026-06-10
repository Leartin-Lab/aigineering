"""Tests for method action sub-contract construction."""

import pytest

from aigineering.core.methods import method_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Contract


def test_plan_action_creates_system_sub_contract():
    parent = Contract(
        id="contract_parent",
        name="root",
        inputs=["evidence"],
        outputs=["report"],
        tool_scope=["search"],
        labels=["research"],
    )
    child = method_contract(
        parent,
        WorkerAction(type="plan", payload={"reason": "split work"}),
    )

    assert child.parent_id == parent.id
    assert child.name == "root.plan"
    assert child.origin == "system"
    assert child.outputs == ["_plan_result_contract_parent"]
    assert child.activation == "_method_ctx_contract_parent"
    assert child.tool_scope == ["search"]
    assert child.labels == ["research"]


def test_method_contract_ids_are_deterministic():
    parent = Contract(id="contract_parent", name="root")
    action = WorkerAction(type="replan", payload={"reason": "blocked"})

    first = method_contract(parent, action)
    second = method_contract(parent, action)

    assert first.id == second.id
    assert first.outputs == ["_replan_result_contract_parent"]


def test_exec_action_is_not_a_method_contract():
    with pytest.raises(ValueError, match="not a method action"):
        method_contract(Contract(id="contract_parent"), WorkerAction(type="exec"))
