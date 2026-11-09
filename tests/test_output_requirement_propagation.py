"""Ordinary planners preserve only the output conditions they declare."""

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.output_satisfaction import output_asset_meets_conditions
from aigineering.plugins.delegation import _parallel_tool_contracts
from aigineering.plugins.task_semantics import _build_plan_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset


def _requirements(definition: str, content: str) -> dict:
    return {"definition_id": definition, "content_id": content}


def test_output_matcher_checks_all_declared_asset_conditions():
    policy = {
        "mode": "mechanical",
        "output_shapes": {"final": {"ok": "boolean"}},
        "output_requirements": {
            "final": _requirements(
                "definition:v1:" + "a" * 64, "content:v1:" + "b" * 64
            )
        },
    }
    contract = build_control_plane_contract(
        name="exact-output", outputs=("final",), acceptance_policy=policy
    )
    asset = Asset(
        id="matching",
        name="final",
        content='{"ok":true}',
        origin="recovery",
        definition_hash="definition:v1:" + "a" * 64,
        content_hash="content:v1:" + "b" * 64,
    )
    wrong_content = Asset(
        **{
            **asset.__dict__,
            "id": "wrong-content",
            "content_hash": "content:v1:" + "c" * 64,
        }
    )
    wrong_shape = Asset(**{**asset.__dict__, "id": "wrong-shape", "content": "[]"})

    assert output_asset_meets_conditions(contract, "final", asset)
    assert not output_asset_meets_conditions(contract, "final", wrong_content)
    assert not output_asset_meets_conditions(contract, "final", wrong_shape)


def test_planned_child_preserves_exact_requirements_for_declared_slot():
    source = build_control_plane_contract(
        name="planner-source",
        outputs=("draft", "final"),
        acceptance_policy={
            "mode": "mechanical",
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


def test_tool_continuation_keeps_parent_output_conditions():
    policy = {
        "mode": "mechanical",
        "output_shapes": {"final": {"type": "string"}},
        "output_requirements": {
            "final": _requirements(
                "definition:v1:" + "c" * 64, "content:v1:" + "d" * 64
            )
        },
        "independent_review": {"policy": "review-v1"},
    }
    parent = build_control_plane_contract(
        name="tool-parent",
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
    assert deep_thaw(continuation.acceptance_policy) == policy
