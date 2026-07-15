"""Replan compatibility handler using the shared planning plugin adapter."""

from __future__ import annotations

from aigineering.core.method_handlers.plan import PlanMethodHandler


class ReplanMethodHandler(PlanMethodHandler):
    """Apply the same contained task publication policy to replan results."""

    action_type = "replan"
    result_prefix = "_replan_result_"
