"""Compatibility handler and exports for plugin-native recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.plugins.recovery import (
    has_recoverable_method_result_rejection,
    schedule_method_result_recovery,
    schedule_projection_recovery,
)
from aigineering.protocol.types import Candidate, Contract

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime


class RecoveryMethodHandler:
    """Compatibility ingress for explicit operator recovery decisions."""

    def handle_cancel(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        candidate: Candidate,
    ) -> bool:
        if candidate.parsed_action is not None:
            action = candidate.parsed_action.get("action")
            if action != "cancel":
                return False
        return runtime.cancel_contract(
            contract,
            reason="operator requested cancellation of recovery-required contract",
            relation_target=candidate.worker_id,
        )


__all__ = (
    "RecoveryMethodHandler",
    "has_recoverable_method_result_rejection",
    "schedule_method_result_recovery",
    "schedule_projection_recovery",
)
