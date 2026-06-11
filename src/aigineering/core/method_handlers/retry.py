"""Retry method handler — creates retry contracts via method dispatch (v0.4.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.ids import hash_retry
from aigineering.protocol.types import Contract

if TYPE_CHECKING:
    from aigineering.core.engine import Engine
    from aigineering.protocol.types import Asset, Candidate


class RetryMethodHandler:
    """Handler for ``retry`` method actions.

    ``handle_method`` creates a new contract with a deterministic retry
    identity (computed from the original contract id via :func:`hash_retry`),
    copying inputs, outputs, and budget from the parent.  No sub-contract
    scheduling is performed — the retry contract is added directly.

    ``handle_completion`` is a no-op for retry (returns False).
    """

    def can_handle(self, action_type: str) -> bool:
        return action_type == "retry"

    def handle_method(
        self,
        engine: Engine,
        contract: Contract,
        action_type: str,
        candidate: Candidate,
    ) -> bool:
        """Create a deterministic retry contract from the triggering contract.

        Returns True to signal the retry was handled (parent is suspended).
        """
        retry_id = hash_retry(contract.id)

        # Avoid duplicate creation when retry contract already exists.
        if engine._store.get_contract(retry_id) is not None:
            return True

        retry_contract = Contract(
            id=retry_id,
            parent_id=contract.parent_id,
            name=contract.name,
            description=contract.description,
            inputs=contract.inputs,
            outputs=contract.outputs,
            activation=contract.activation,
            budget=contract.budget,
            tool_scope=contract.tool_scope,
            labels=contract.labels,
            origin=contract.origin,
            sensitive_input_policy=contract.sensitive_input_policy,
        )
        engine._store.add_contract(retry_contract)
        engine._budget[retry_id] = max(retry_contract.budget, 1)

        engine._add_trace(
            contract.id,
            "retry_created",
            relation_type="retry",
            relation_target=retry_id,
            budget_remaining=engine._resolve_budget(contract),
        )
        return True

    def handle_completion(
        self,
        engine: Engine,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool:
        """Retry has no method sub-contract to complete. Returns False."""
        return False
