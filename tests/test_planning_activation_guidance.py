from __future__ import annotations

import json

import pytest

from aigineering.agent.prompt import contract_prompt
from aigineering.plugins.base import PluginRequest
from aigineering.plugins.staged_planning import (
    StagedPlanningPlugin,
    StagedReplanningPlugin,
)
from aigineering.plugins.task_semantics import contracts_from_plan_asset
from aigineering.protocol.types import Asset, Contract


@pytest.mark.parametrize(
    ("plugin", "mode"),
    [
        (StagedPlanningPlugin(), "plan"),
        (StagedReplanningPlugin(), "replan"),
    ],
    ids=("planning", "replanning"),
)
def test_each_planning_stage_prompt_explains_activation_asset_semantics(plugin, mode):
    parent = Contract(
        id="task:v3:activation-guidance-parent",
        name="activation-guidance",
        description="Produce a report from disclosed evidence.",
        inputs=("source",),
        outputs=("report",),
        budget=9,
    )
    stages = plugin.stages(
        PluginRequest(
            parent=parent,
            allowed_input_names=frozenset({"source"}),
            allowance=9,
        )
    )

    assert len(stages.contracts) == 3
    for stage_name, contract in zip(
        ("draft", "dependencies", "compile"), stages.contracts
    ):
        prompt = contract_prompt(contract, [])
        assert f"plugin:{mode}.{stage_name}" in contract.labels
        assert "Asset-availability expression" in prompt
        assert "uppercase AND/OR and parentheses" in prompt
        assert "NOT is unsupported" in prompt
        assert "empty string adds no activation condition" in prompt
        assert "declared inputs are still required" in prompt
        assert "no built-in lifecycle events or constants" in prompt
        assert "work only if they name actual allowed Assets" in prompt
        assert "Reference only parent inputs or reachable sibling outputs" in prompt


def _parent(*, inputs: tuple[str, ...] = ()) -> Contract:
    return Contract(
        id="task:v3:activation-compiler-parent",
        name="activation-compiler-parent",
        description="Produce the required report.",
        inputs=inputs,
        outputs=("report",),
        budget=4,
    )


def _plan_asset(*, name: str, inputs: list[str], activation: str) -> Asset:
    return Asset(
        id="asset:activation-plan",
        name="planning_blueprint",
        content=json.dumps(
            {
                "contracts": [
                    {
                        "name": "publish-report",
                        "description": "Publish the required report.",
                        "inputs": inputs,
                        "outputs": ["report"],
                        "activation": activation,
                        "budget": 1,
                    }
                ]
            }
        ),
    )


def test_compiler_rejects_unavailable_on_start_activation_with_recovery_fields():
    contracts, rejections = contracts_from_plan_asset(
        _plan_asset(name="report", inputs=[], activation="on_start"),
        parent_id="task:v3:activation-compiler-parent",
        parent_contract=_parent(),
        allowed_input_names=set(),
        parent_budget_remaining=4,
    )

    assert contracts == []
    assert {item["field"] for item in rejections} >= {
        "dependencies",
        "output_recommitment",
    }


@pytest.mark.parametrize("asset_name", ["on_start", "true"])
def test_compiler_accepts_lifecycle_like_names_when_they_are_allowed_assets(asset_name):
    source = Asset(id=f"asset:{asset_name}", name=asset_name, content="available")
    contracts, rejections = contracts_from_plan_asset(
        _plan_asset(name="report", inputs=[asset_name], activation=asset_name),
        parent_id="task:v3:activation-compiler-parent",
        parent_contract=_parent(inputs=(asset_name,)),
        allowed_input_names={asset_name},
        parent_budget_remaining=4,
        context_assets=(source,),
    )

    assert not rejections
    assert len(contracts) == 1
    assert contracts[0].inputs == (asset_name,)
    assert contracts[0].activation == asset_name


def test_ordinary_task_prompt_has_no_planning_stage_activation_guidance():
    prompt = contract_prompt(_parent(), [])

    assert "Asset-availability expression" not in prompt
    assert "no built-in lifecycle events or constants" not in prompt
