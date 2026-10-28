"""Default planned tool budgets satisfy the continuation minimum."""

from __future__ import annotations

import json

import pytest

from aigineering.agent.prompt import contract_prompt
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import hash_contract_v3
from aigineering.plugins import PlanningExpansionPlugin, PluginRequest
from aigineering.plugins.planning import compile_planning_blueprint
from aigineering.protocol.types import Asset, Contract


def _parent(*, budget: int = 8) -> Contract:
    fields = {
        "name": "deliver_report",
        "description": "Deliver a report",
        "inputs": ("source",),
        "outputs": ("final_report",),
        "activation": "source",
        "budget": budget,
        "tool_scope": ("lookup",),
        "labels": (),
        "origin": "human",
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _legacy_blueprint(
    *,
    budget: object = None,
    include_budget: bool = False,
    tool_scope: tuple[str, ...] = ("lookup",),
    capability_needs: tuple[str, ...] = (),
) -> str:
    child = {
        "name": "research",
        "description": "Use the permitted tool and produce the report.",
        "inputs": ["source"],
        "outputs": ["final_report"],
        "activation": "source",
        "tool_scope": list(tool_scope),
        "capability_needs": list(capability_needs),
    }
    if include_budget:
        child["budget"] = budget
    return json.dumps({"contracts": [child]}, sort_keys=True)


def _scaffold_blueprint(
    *,
    include_budget: bool = False,
    budget: object = 1,
    tool_scope: tuple[str, ...] = ("lookup",),
) -> str:
    task = {
        "name": "research",
        "description": "Use the permitted tool and produce the report.",
        "consumes": ["source"],
        "produces": ["final_report"],
        "tool_scope": list(tool_scope),
    }
    if include_budget:
        task["budget"] = budget
    return json.dumps(
        {
            "intermediate_assets": [],
            "step_1_tasks": [task],
            "step_2_data_flow": [
                {
                    "task_name": "research",
                    "consumes": ["source"],
                    "produces": ["final_report"],
                }
            ],
            "step_3_activation": [
                {
                    "task_name": "research",
                    "expression": "source",
                    "depends_on": ["source"],
                }
            ],
        },
        sort_keys=True,
    )


def _proposal(blueprint: str, *, allowance: int = 8, parent: Contract | None = None):
    return PlanningExpansionPlugin().propose(
        PluginRequest(
            parent=parent or _parent(),
            assets=(Asset(id="plan-asset", name="plan", content=blueprint),),
            allowed_input_names=frozenset({"source"}),
            allowance=allowance,
        )
    )


@pytest.mark.parametrize("blueprint", [_legacy_blueprint(), _scaffold_blueprint()])
def test_omitted_tool_budget_defaults_to_two_for_both_planning_formats(blueprint):
    proposal = _proposal(blueprint)

    assert proposal.rejections == ()
    assert len(proposal.effects) == 1
    assert proposal.effects[0].payload["contract"]["budget"] == 2


@pytest.mark.parametrize(
    "blueprint",
    [
        _legacy_blueprint(tool_scope=()),
        _scaffold_blueprint(tool_scope=()),
    ],
)
def test_omitted_non_tool_budget_remains_one(blueprint):
    proposal = _proposal(blueprint)

    assert proposal.rejections == ()
    assert proposal.effects[0].payload["contract"]["budget"] == 1


@pytest.mark.parametrize(
    "blueprint",
    [
        _legacy_blueprint(budget=1, include_budget=True),
        _scaffold_blueprint(include_budget=True),
    ],
)
def test_explicit_tool_budget_one_is_still_rejected(blueprint):
    proposal = _proposal(blueprint)

    assert proposal.effects == ()
    assert any(
        item.get("field") == "budget"
        and "requires budget >= 2" in item.get("reason", "")
        for item in proposal.rejections
    )


@pytest.mark.parametrize(
    "blueprint",
    [
        _legacy_blueprint(budget=0, include_budget=True),
        _scaffold_blueprint(include_budget=True, budget=0),
    ],
)
def test_explicit_invalid_tool_budget_keeps_existing_rejection_behavior(blueprint):
    proposal = _proposal(blueprint)

    assert proposal.effects == ()
    assert any(
        item.get("field") == "budget"
        and item.get("actual") == "1"
        and "requires budget >= 2" in item.get("reason", "")
        for item in proposal.rejections
    )


@pytest.mark.parametrize(
    "blueprint",
    [
        _legacy_blueprint(budget=4, include_budget=True),
        _scaffold_blueprint(include_budget=True, budget=4),
    ],
)
def test_explicit_tool_budget_is_contained_without_being_increased(blueprint):
    proposal = _proposal(blueprint, allowance=3)

    assert len(proposal.effects) == 1
    assert proposal.effects[0].payload["contract"]["budget"] == 3
    assert any(item.get("action") == "budget_contained" for item in proposal.rejections)


@pytest.mark.parametrize("blueprint", [_legacy_blueprint(), _scaffold_blueprint()])
def test_tool_default_is_rejected_when_parent_has_less_than_two_allowance(blueprint):
    proposal = _proposal(blueprint, allowance=1)

    assert proposal.effects == ()
    assert any(
        item.get("field") == "budget"
        and "requires budget >= 2" in item.get("reason", "")
        for item in proposal.rejections
    )


def test_tool_child_is_rejected_when_earlier_sibling_leaves_only_one_unit():
    blueprint = json.dumps(
        {
            "contracts": [
                {
                    "name": "prepare",
                    "description": "Prepare the tool input.",
                    "inputs": ["source"],
                    "outputs": ["prepared"],
                    "activation": "source",
                    "budget": 1,
                },
                {
                    "name": "research",
                    "description": "Use the permitted tool to produce the report.",
                    "inputs": ["prepared"],
                    "outputs": ["final_report"],
                    "activation": "prepared",
                    "tool_scope": ["lookup"],
                },
            ]
        },
        sort_keys=True,
    )

    proposal = _proposal(blueprint, allowance=2)

    assert proposal.effects == ()
    matching_rejections = [
        item
        for item in proposal.rejections
        if item.get("child_name") == "research"
        and item.get("field") == "budget"
        and item.get("action") == "rejected"
    ]
    assert len(matching_rejections) == 1
    assert matching_rejections[0]["requested"] == 2
    assert matching_rejections[0]["effective"] == 0
    assert matching_rejections[0]["remaining"] == 1


def test_tool_default_is_rejected_when_parent_contract_budget_is_one():
    proposal = _proposal(_legacy_blueprint(), allowance=8, parent=_parent(budget=1))

    assert proposal.effects == ()
    assert any(
        item.get("field") == "budget"
        and item.get("actual") == "1"
        and "requires budget >= 2" in item.get("reason", "")
        for item in proposal.rejections
    )


def test_v6_planning_compile_defaults_omitted_tool_budget_to_two():
    parent = build_control_plane_contract(
        name="v6-parent",
        description=json.dumps({"allowed_inputs": ["source"]}),
        inputs=("source",),
        outputs=("final_report",),
        budget=8,
        tool_scope=("lookup",),
        delegation_capabilities=("tool_calling",),
        sensitive_input_policy={"input_asset_bindings": []},
    )

    assert parent.id.startswith("task:v6:")
    effects = compile_planning_blueprint(
        parent,
        {"planning_blueprint": _legacy_blueprint(capability_needs=("tool_calling",))},
        allowance=8,
    )

    assert len(effects) == 1
    assert effects[0].payload["contract"]["budget"] == 2


def test_planning_compile_prompt_states_tool_and_future_work_budget_rules():
    contract = Contract(
        id="plan-compile",
        name="compile",
        description=json.dumps({"allowed_inputs": ["source"]}),
        inputs=("source",),
        outputs=("planning_blueprint",),
        budget=8,
        labels=("plugin:plan.compile",),
    )

    prompt = contract_prompt(contract, [])

    assert "default to budget 1 only for a child with empty tool_scope" in prompt
    assert "non-empty tool_scope requires budget >= 2" in prompt
    assert "A child with further tool calls" in prompt
    assert "explicitly budget" in prompt
