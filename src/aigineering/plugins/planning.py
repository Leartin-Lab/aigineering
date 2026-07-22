"""Planning expansion as a pure task-producing plugin."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.plugins.base import PluginProposal, PluginRequest
from aigineering.plugins.task_semantics import contracts_from_plan_asset
from aigineering.protocol.candidate import CandidateEffect
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Asset, Contract


_BLOCKING_REJECTION_ACTIONS = frozenset({"rejected", "scaffold_rejected"})


class PlanningCompileError(ValueError):
    """A compile-stage blueprint cannot become contained task effects."""

    def __init__(self, message: str, *, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = tuple(sorted(set(fields)))


def is_blocking_plan_rejection(entry: dict) -> bool:
    """Return whether one planner diagnostic invalidates the atomic fan-out."""
    return entry.get("action") in _BLOCKING_REJECTION_ACTIONS


class PlanningExpansionPlugin:
    """Convert one structured plan Asset into contained ordinary tasks."""

    plugin_id = "planning.expand.v1"

    def propose(self, request: PluginRequest) -> PluginProposal:
        if len(request.assets) != 1:
            raise ValueError("planning expansion requires exactly one plan Asset")
        contracts, rejections = contracts_from_plan_asset(
            request.assets[0],
            request.parent.id,
            parent_contract=request.parent,
            allowed_input_names=set(request.allowed_input_names),
            parent_budget_remaining=request.allowance,
        )
        blocking = any(is_blocking_plan_rejection(item) for item in rejections)
        if not contracts and not rejections:
            rejections.append(
                {
                    "child_name": "(plan_result)",
                    "field": "contracts",
                    "reason": "plan result produced no executable child tasks",
                    "action": "rejected",
                    "recoverable": True,
                }
            )
            blocking = True
        return PluginProposal(
            effects=(
                ()
                if blocking
                else tuple(contract_declaration_effect(item) for item in contracts)
            ),
            rejections=tuple(rejections),
        )


def compile_planning_blueprint(
    contract: Contract,
    outputs: Mapping[str, object],
    *,
    allowance: int,
) -> tuple[CandidateEffect, ...]:
    """Compile one Worker-local blueprint into ordinary child declarations."""
    if set(outputs) != {"planning_blueprint"}:
        raise PlanningCompileError(
            "planning compile must return exactly one structured blueprint",
            fields=("outputs",),
        )
    content = next(iter(outputs.values()))
    if not isinstance(content, str) or not content.strip():
        raise PlanningCompileError(
            "planning blueprint must be non-empty", fields=("outputs",)
        )
    try:
        description = json.loads(contract.description)
    except json.JSONDecodeError as exc:
        raise PlanningCompileError(
            "compile Contract description is invalid", fields=("description",)
        ) from exc
    allowed_inputs = description.get("allowed_inputs", contract.inputs)
    plan_asset = Asset(
        id=hash_asset_content("planning_blueprint", content),
        name="planning_blueprint",
        content=content,
        created_by=contract.id,
        definition_hash=hash_asset_definition("planning_blueprint"),
        content_hash=hash_asset_content("planning_blueprint", content),
    )
    proposal = PlanningExpansionPlugin().propose(
        PluginRequest(
            parent=contract,
            assets=(plan_asset,),
            allowed_input_names=frozenset(str(name) for name in allowed_inputs),
            allowance=allowance,
        )
    )
    if not proposal.effects:
        reasons = "; ".join(
            str(item.get("reason", "planning expansion rejected"))
            for item in proposal.rejections
        )
        raise PlanningCompileError(
            reasons,
            fields=tuple(
                str(item.get("field", "unknown")) for item in proposal.rejections
            ),
        )
    return proposal.effects
