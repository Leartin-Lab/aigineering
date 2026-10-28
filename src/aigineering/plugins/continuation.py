"""Continuation task publication as a pure plugin."""

from __future__ import annotations

from collections.abc import Mapping

from aigineering.plugins.task_review import is_task_review_source_eligible
from aigineering.plugins.task_semantics import continuation_contract, method_payload
from aigineering.plugins.base import PluginProposal, PluginRequest
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract


def allocate_continuation_budget(
    parent: Contract, available: int, *, recovery_demand: int = 0
) -> tuple[int, int]:
    """Allocate a positive continuation budget and retain bounded parent work."""
    if type(available) is not int or available < 0:
        raise ValueError("continuation allowance must be a non-negative integer")
    if type(recovery_demand) is not int or recovery_demand < 0:
        raise ValueError("continuation recovery demand must be a non-negative integer")

    policy = parent.acceptance_policy
    review_demand = (
        len(parent.outputs)
        if is_task_review_source_eligible(parent)
        and isinstance(policy, Mapping)
        and policy.get("mode") == "independent"
        else 0
    )
    retained = min(review_demand + recovery_demand, max(0, available - 1))
    return max(1, available - retained), retained


class ContinuationTaskPlugin:
    """Propose one ordinary follow-up task after a method task completes."""

    plugin_id = "continuation.publish.v1"

    def propose(self, request: PluginRequest) -> PluginProposal:
        if request.source is None:
            raise ValueError("continuation publication requires a source Contract")
        method = str(method_payload(request.source).get("method", "method"))
        continuation_budget, _retained = allocate_continuation_budget(
            request.parent, request.allowance
        )
        continuation = continuation_contract(
            request.parent,
            request.source,
            method=method,
            budget=continuation_budget,
        )
        return PluginProposal(effects=(contract_declaration_effect(continuation),))
