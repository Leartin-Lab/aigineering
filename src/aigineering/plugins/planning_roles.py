"""Shared recognition of the active staged-planning Worker role."""

from __future__ import annotations

import json
from collections.abc import Iterable

from aigineering.protocol.types import Contract

PLANNING_STAGE_LABELS = frozenset(
    {
        "plugin:plan.draft",
        "plugin:plan.dependencies",
        "plugin:plan.compile",
        "plugin:replan.draft",
        "plugin:replan.dependencies",
        "plugin:replan.compile",
    }
)
PLANNING_COMPILE_LABELS = frozenset({"plugin:plan.compile", "plugin:replan.compile"})
_METHOD_ROLES = frozenset({"tool", "fail", "parallel_tool", "parallel_tool_item"})
_METHOD_LABELS = {
    "tool": frozenset({"plugin:tool", "plugin:parallel_tool_item", "method:tool"}),
    "fail": frozenset({"plugin:fail", "method:fail"}),
    "parallel_tool": frozenset({"plugin:parallel_tool", "method:parallel_tool"}),
    "parallel_tool_item": frozenset({"plugin:parallel_tool_item"}),
}


def without_planning_stage_labels(labels: Iterable[str]) -> tuple[str, ...]:
    """Drop only the six stage-control labels, preserving order and other labels."""
    return tuple(label for label in labels if label not in PLANNING_STAGE_LABELS)


def active_planning_stage(contract: Contract) -> str | None:
    """Return the one active stage label, excluding system method subtasks.

    Older method Contracts may contain an inherited stage label. Their typed
    method description and system/recovery origin identify them as method work,
    so that historical shape cannot route their output through a stage compiler.
    """
    if contract.origin in {"system", "recovery", "retry"} and _is_method_contract(
        contract
    ):
        return None

    labels = tuple(label for label in contract.labels if label in PLANNING_STAGE_LABELS)
    if len(labels) == 1:
        return labels[0]
    if not labels:
        return None

    description = _description(contract)
    mode = description.get("mode")
    stage = description.get("stage")
    if (
        isinstance(mode, str)
        and mode in {"plan", "replan"}
        and isinstance(stage, str)
        and stage
        in {
            "draft",
            "dependencies",
            "compile",
        }
    ):
        current = f"plugin:{mode}.{stage}"
        if current in labels:
            return current
    if (
        "mode" not in description
        and isinstance(stage, str)
        and stage
        in {
            "draft",
            "dependencies",
            "compile",
        }
    ):
        current = tuple(label for label in labels if label.endswith(f".{stage}"))
        if len(current) == 1:
            return current[0]
    compile_labels = tuple(
        label for label in labels if label in PLANNING_COMPILE_LABELS
    )
    if compile_labels:
        # Ambiguous legacy labels must keep the blueprint compiler fence active.
        return (
            "plugin:replan.compile"
            if "plugin:replan.compile" in compile_labels
            else "plugin:plan.compile"
        )
    return None


def _is_method_contract(contract: Contract) -> bool:
    description = _description(contract)
    method = description.get("method")
    return (
        isinstance(method, str)
        and method in _METHOD_ROLES
        and bool(set(contract.labels) & _METHOD_LABELS.get(method, frozenset()))
    )


def _description(contract: Contract) -> dict:
    try:
        value = json.loads(contract.description)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
