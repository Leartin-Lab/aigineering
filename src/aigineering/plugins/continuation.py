"""Bounded allowance allocation for ordinary continuation tasks."""

from __future__ import annotations

from collections.abc import Mapping

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
        if isinstance(policy, Mapping) and policy.get("mode") == "independent"
        else 0
    )
    retained = min(review_demand + recovery_demand, max(0, available - 1))
    return max(1, available - retained), retained
