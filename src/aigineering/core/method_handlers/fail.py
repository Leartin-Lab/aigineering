"""Legacy Engine scheduling adapter for explicit failure tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.plugins.fail_completion import FailCompletionPlugin
from aigineering.protocol.actions import parse_method_action

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime
    from aigineering.protocol.types import Candidate, Contract


class FailMethodHandler(FailCompletionPlugin):
    """Compatibility adapter retained only for the source-only legacy Engine."""

    def handle_method(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        action_type: str,
        candidate: Candidate,
    ) -> bool:
        del action_type
        action = parse_method_action(candidate)
        if action is None:
            return False
        runtime.schedule_method(contract, action, candidate)
        return True
