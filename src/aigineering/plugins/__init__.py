"""Task-producing plugins outside the runtime kernel."""

from aigineering.plugins.base import PluginProposal, PluginRequest, TaskPlugin
from aigineering.plugins.continuation import ContinuationTaskPlugin
from aigineering.plugins.planning import PlanningExpansionPlugin

__all__ = (
    "PlanningExpansionPlugin",
    "ContinuationTaskPlugin",
    "PluginProposal",
    "PluginRequest",
    "TaskPlugin",
)
