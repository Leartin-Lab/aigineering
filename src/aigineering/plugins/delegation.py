"""Worker task delegation as a typed Candidate adapter plugin."""

from __future__ import annotations

from aigineering.protocol.actions import parse_method_action
from aigineering.protocol.effect_builders import task_delegation_effect
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Candidate


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
