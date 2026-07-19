"""Task-producing plugins outside the runtime kernel."""

from aigineering.plugins.base import PluginProposal, PluginRequest, TaskPlugin
from aigineering.plugins.completion import CompletionPlugin, CompletionRegistry
from aigineering.plugins.completion_projection import (
    TaskCompletionContext,
    TaskCompletionProjector,
)
from aigineering.plugins.continuation import ContinuationTaskPlugin
from aigineering.plugins.delegation import DelegationProjection, TaskDelegationPlugin
from aigineering.plugins.fail_completion import FailCompletionPlugin
from aigineering.plugins.planning import PlanningExpansionPlugin
from aigineering.plugins.planning_completion import (
    PlanningCompletionPlugin,
    ReplanningCompletionPlugin,
)
from aigineering.plugins.tool_completion import ToolCompletionPlugin


def default_completion_registry() -> CompletionRegistry:
    """Compose the supported completion plugins without legacy handlers."""
    registry = CompletionRegistry()
    registry.register("plan", PlanningCompletionPlugin())
    registry.register("replan", ReplanningCompletionPlugin())
    registry.register("tool", ToolCompletionPlugin())
    registry.register("fail", FailCompletionPlugin())
    return registry


__all__ = (
    "PlanningExpansionPlugin",
    "PlanningCompletionPlugin",
    "ContinuationTaskPlugin",
    "CompletionPlugin",
    "CompletionRegistry",
    "TaskCompletionContext",
    "TaskCompletionProjector",
    "default_completion_registry",
    "DelegationProjection",
    "FailCompletionPlugin",
    "PluginProposal",
    "PluginRequest",
    "ReplanningCompletionPlugin",
    "TaskPlugin",
    "ToolCompletionPlugin",
    "TaskDelegationPlugin",
)
