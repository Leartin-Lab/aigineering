"""Task-producing plugins outside the runtime kernel."""

from aigineering.plugins.base import PluginProposal, PluginRequest, TaskPlugin
from aigineering.plugins.completion import CompletionPlugin, CompletionRegistry
from aigineering.plugins.continuation import ContinuationTaskPlugin
from aigineering.plugins.delegation import DelegationProjection, TaskDelegationPlugin
from aigineering.plugins.planning import PlanningExpansionPlugin

__all__ = (
    "PlanningExpansionPlugin",
    "ContinuationTaskPlugin",
    "CompletionPlugin",
    "CompletionRegistry",
    "DelegationProjection",
    "PluginProposal",
    "PluginRequest",
    "TaskPlugin",
    "TaskDelegationPlugin",
)
