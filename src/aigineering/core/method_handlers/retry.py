"""Retry method handler — creates retry contracts via method dispatch (v0.4.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.methods import retry_contract
from aigineering.protocol.types import Contract

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime
    from aigineering.protocol.types import Asset, Candidate


class RetryMethodHandler:
    """Handler for ``retry`` method actions.

    ``handle_method`` creates a new contract with a deterministic retry
    v3 identity bound to the inherited security/routing policy,
    copying inputs, outputs, and budget from the parent.  No sub-contract
    scheduling is performed — the retry contract is added directly.

    ``handle_completion`` is a no-op for retry (returns False).
    """

    def can_handle(self, action_type: str) -> bool:
        return action_type == "retry"

    def handle_method(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        action_type: str,
        candidate: Candidate,
    ) -> bool:
        """Create a deterministic retry contract from the triggering contract.

        Returns True to signal the retry was handled (parent is suspended).
        """
        retry = retry_contract(contract)

        # Avoid duplicate creation when retry contract already exists.
        if runtime.get_contract(retry.id) is not None:
            return True
        runtime.add_contract(retry)

        runtime.append_trace(
            contract.id,
            "retry_created",
            relation_type="retry",
            relation_target=retry.id,
            budget_remaining=runtime.resolve_budget(contract.id),
        )
        return True

    def handle_completion(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool:
        """Retry has no method sub-contract to complete. Returns False."""
        return False
