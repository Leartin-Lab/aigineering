"""Output conditions follow only the successor's declared output roles."""

from __future__ import annotations

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.delegation import _parallel_tool_contracts
from aigineering.plugins.task_semantics import _build_plan_contract
from aigineering.plugins.task_successors import (
    satisfied_asset_ids,
    satisfied_output_slots,
    build_successor_contract,
)
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset


def _requirements(definition: str, content: str) -> dict:
    return {
        "definition_id": definition,
        "content_id": content,
    }


def test_slot_matching_uses_complete_multi_output_contract():
    requirements = {
        "draft": _requirements("definition:v1:" + "a" * 64, "content:v1:" + "b" * 64),
        "final": _requirements("definition:v1:" + "c" * 64, "content:v1:" + "d" * 64),
    }
    contract = build_control_plane_contract(
        name="multi-slot",
        outputs=("draft", "final"),
        acceptance_policy={"mode": "mechanical", "output_requirements": requirements},
    )
    draft = Asset(
        id="draft-asset",
        name="draft",
        content="body",
        origin="human",
        definition_hash="definition:v1:" + "a" * 64,
        content_hash="content:v1:" + "b" * 64,
    )

    context = EffectProjectionContext(contracts=(contract,), assets=(draft,))

    assert satisfied_output_slots(contract, context) == (("draft", ("draft-asset",)),)
    assert satisfied_asset_ids(contract, "draft", context) == ("draft-asset",)


def test_successor_filters_role_conditions_and_drops_retired_tree_source():
    policy = {
        "mode": "mechanical",
        "output_source": "contract_tree",
        "output_shapes": {
            "draft": {"type": "string"},
            "final": {"type": "object"},
        },
        "output_requirements": {
            "draft": _requirements(
                "definition:v1:" + "a" * 64, "content:v1:" + "b" * 64
            ),
            "final": _requirements(
                "definition:v1:" + "c" * 64, "content:v1:" + "d" * 64
            ),
        },
        "independent_review": {"policy": "review-v1"},
    }
    source = build_control_plane_contract(
        name="source", outputs=("draft", "final"), acceptance_policy=policy
    )

    successor = build_successor_contract(
        source, ("final",), 4, "continue", name_suffix=".next"
    )

    assert deep_thaw(successor.acceptance_policy) == {
        "mode": "mechanical",
        "output_shapes": {"final": {"type": "object"}},
        "output_requirements": {
            "final": _requirements(
                "definition:v1:" + "c" * 64, "content:v1:" + "d" * 64
            )
        },
        "independent_review": {"policy": "review-v1"},
    }


def test_plan_contract_filters_shapes_and_requirements_without_tree_gate():
    source = build_control_plane_contract(
        name="planner-source",
        outputs=("draft", "final"),
        acceptance_policy={
            "mode": "mechanical",
            "output_source": "contract_tree",
            "output_shapes": {
                "draft": {"type": "string"},
                "final": {"type": "object"},
            },
            "output_requirements": {
                "draft": _requirements(
                    "definition:v1:" + "a" * 64, "content:v1:" + "b" * 64
                ),
                "final": _requirements(
                    "definition:v1:" + "c" * 64, "content:v1:" + "d" * 64
                ),
            },
        },
    )

    child = _build_plan_contract(
        parent_id=source.id,
        parent_contract=source,
        name="final-writer",
        description="Write the final output.",
        inputs=[],
        outputs=["final"],
        activation="",
        budget=2,
        tool_scope=[],
        labels=[],
        capability_needs=[],
        pool_needs=[],
        delegation_capabilities=[],
        delegation_pools=[],
        context_assets=(),
    )

    assert deep_thaw(child.acceptance_policy) == {
        "mode": "mechanical",
        "output_shapes": {"final": {"type": "object"}},
        "output_requirements": {
            "final": _requirements(
                "definition:v1:" + "c" * 64, "content:v1:" + "d" * 64
            )
        },
    }


def test_parallel_continuation_keeps_policy_except_retired_source():
    policy = {
        "mode": "mechanical",
        "output_source": "contract_tree",
        "output_shapes": {"final": {"type": "string"}},
        "output_requirements": {
            "final": _requirements(
                "definition:v1:" + "c" * 64, "content:v1:" + "d" * 64
            )
        },
        "independent_review": {"policy": "review-v1"},
    }
    parent = build_control_plane_contract(
        name="parallel-parent",
        outputs=("final",),
        budget=8,
        tool_scope=("lookup", "lookup2"),
        acceptance_policy=policy,
    )
    action = WorkerAction(
        type="parallel_tool",
        payload={
            "calls": [
                {"id": "one", "name": "lookup", "args": {}},
                {"id": "two", "name": "lookup2", "args": {}},
            ]
        },
    )

    continuation = _parallel_tool_contracts(parent, action, allowance=8)[-1]

    assert continuation.outputs == parent.outputs
    assert deep_thaw(continuation.acceptance_policy) == {
        key: value for key, value in policy.items() if key != "output_source"
    }
