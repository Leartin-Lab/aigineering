"""Completion projection for worker-produced tool observations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.plugins.task_semantics import method_payload

if TYPE_CHECKING:
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.protocol.types import Asset, Contract


class ToolCompletionPlugin:
    """Acknowledge a declared tool observation for continuation projection."""

    action_type = "tool"

    def can_handle(self, action_type: str) -> bool:
        return action_type == self.action_type

    def handle_completion(
        self,
        runtime: TaskCompletionContext,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool:
        del runtime
        if method_payload(contract).get("method") != self.action_type:
            return False
        return any(asset.name in contract.outputs for asset in method_assets)
