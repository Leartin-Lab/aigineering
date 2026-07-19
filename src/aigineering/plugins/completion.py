"""Minimal registry for plugins that project completed system tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.protocol.types import Asset, Contract


class CompletionPlugin(Protocol):
    """Project one completed system task into its visible consequences."""

    def can_handle(self, action_type: str) -> bool: ...

    def handle_completion(
        self,
        runtime: TaskCompletionContext,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool: ...


class CompletionRegistry:
    """Small action-type lookup with no task-publication API."""

    def __init__(self) -> None:
        self._plugins: dict[str, CompletionPlugin] = {}

    def register(self, action_type: str, plugin: CompletionPlugin) -> None:
        if not action_type:
            raise ValueError("completion action_type must not be empty")
        self._plugins[action_type] = plugin

    def get(self, action_type: str) -> CompletionPlugin | None:
        return self._plugins.get(action_type)

    def list_types(self) -> list[str]:
        return sorted(self._plugins)
