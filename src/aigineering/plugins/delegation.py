"""Worker task delegation as a typed Candidate adapter plugin."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.core.methods import (
    method_context_content,
    method_contract,
    retry_contract,
    system_asset,
)
from aigineering.core.provenance import sign_asset
from aigineering.protocol.actions import WorkerAction, parse_method_action
from aigineering.protocol.effect_builders import task_delegation_effect
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Asset, Candidate, Contract


@dataclass(frozen=True)
class DelegationProjection:
    """Pure task/context consequences of one authorized delegation request."""

    child: Contract
    context_asset: Asset | None
    event_type: str


class TaskDelegationPlugin:
    """Convert one explicit worker method action into ``task.delegate``."""

    plugin_id = "task.delegate.v1"

    def propose(self, envelope: CandidateEnvelope):
        candidate = Candidate(
            worker_id=envelope.worker_id,
            raw_output=envelope.raw_output,
            parsed_action=envelope.parsed_action,
            metadata=envelope.usage_metadata,
        )
        if parse_method_action(candidate) is None:
            raise ValueError("task delegation requires an explicit method action")
        return task_delegation_effect(envelope)

    def project(
        self,
        parent: Contract,
        action: WorkerAction,
    ) -> DelegationProjection:
        """Derive contained task facts without Store access or mutation."""
        if action.type == "retry":
            return DelegationProjection(retry_contract(parent), None, "retry_created")
        child = method_contract(parent, action)
        context_asset = sign_asset(
            system_asset(
                name=f"_method_ctx_{parent.id}",
                content=method_context_content(parent, action, child),
                created_by=parent.id,
            )
        )
        return DelegationProjection(child, context_asset, "method_scheduled")
