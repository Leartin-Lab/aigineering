"""Task-producing plugins outside the runtime kernel."""

from aigineering.plugins.base import PluginProposal, PluginRequest, TaskPlugin
from aigineering.plugins.completion import CompletionPlugin, CompletionRegistry
from aigineering.plugins.continuation import ContinuationTaskPlugin
from aigineering.plugins.delegation import DelegationProjection, TaskDelegationPlugin
from aigineering.plugins.fail_completion import FailCompletionPlugin
from aigineering.plugins.planning import PlanningExpansionPlugin
from aigineering.plugins.planning_completion import (
    PlanningCompletionPlugin,
    ReplanningCompletionPlugin,
)
from aigineering.plugins.tool_completion import ToolCompletionPlugin

__all__ = (
    "PlanningExpansionPlugin",
    "PlanningCompletionPlugin",
    "ContinuationTaskPlugin",
    "CompletionPlugin",
    "CompletionRegistry",
    "DelegationProjection",
    "FailCompletionPlugin",
    "PluginProposal",
    "PluginRequest",
    "ReplanningCompletionPlugin",
    "TaskPlugin",
    "ToolCompletionPlugin",
    "TaskDelegationPlugin",
)
