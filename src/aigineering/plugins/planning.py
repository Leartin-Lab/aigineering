"""Planning expansion as a pure task-producing plugin."""

from __future__ import annotations

from aigineering.plugins.base import PluginProposal, PluginRequest
from aigineering.plugins.task_semantics import contracts_from_plan_asset
from aigineering.protocol.effect_builders import contract_declaration_effect


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
        blocking = any(item.get("action") == "rejected" for item in rejections)
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
