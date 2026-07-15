"""Worker protocol for candidate-producing execution environments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aigineering.core.signing import Signer
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateProposal,
    GenesisManifest,
    create_candidate_proposal,
)
from aigineering.protocol.actions import parse_method_action
from aigineering.protocol.effect_builders import worker_output_effect
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Asset, Candidate, Contract


class WorkerExecutionError(ValueError):
    """Expected, safely reportable failure before Candidate production."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@runtime_checkable
class Worker(Protocol):
    """Execution environment that returns candidates, never committed facts."""

    worker_id: str

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate: ...


@dataclass(frozen=True)
class WorkerHost:
    """Bind an execution adapter to one authenticated Candidate actor key."""

    worker: Worker
    genesis: GenesisManifest
    actor_key: ActorKey
    signer: Signer

    def __post_init__(self) -> None:
        if self.actor_key.actor_id != self.worker.worker_id:
            raise ValueError("WorkerHost actor_id must equal worker.worker_id")
        if (
            self.actor_key.kind != self.signer.kind
            or self.actor_key.public_key != self.signer.signer_id
        ):
            raise ValueError("WorkerHost signer does not match actor key")
        if "worker.submit" not in self.actor_key.capabilities:
            raise ValueError("WorkerHost actor key requires 'worker.submit'")

    @property
    def worker_id(self) -> str:
        return self.actor_key.actor_id

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        candidate = self.worker.invoke(contract, disclosed_assets)
        if candidate.worker_id != self.worker_id:
            raise WorkerExecutionError(
                "identity_mismatch",
                "worker output identity does not match its WorkerHost actor",
            )
        return candidate

    def sign_envelope(self, envelope: CandidateEnvelope) -> CandidateProposal:
        if envelope.worker_id != self.worker_id:
            raise ValueError("WorkerHost can sign only its own worker envelope")
        envelope_candidate = Candidate(
            worker_id=envelope.worker_id,
            raw_output=envelope.raw_output,
            parsed_action=envelope.parsed_action,
            metadata=envelope.usage_metadata,
        )
        if parse_method_action(envelope_candidate) is not None:
            from aigineering.plugins import TaskDelegationPlugin

            effect = TaskDelegationPlugin().propose(envelope)
        else:
            effect = worker_output_effect(envelope)
        return create_candidate_proposal(
            domain_id=self.genesis.id,
            actor_id=self.actor_key.actor_id,
            key_id=self.actor_key.key_id,
            effects=(effect,),
            signer=self.signer,
            idempotency_key=envelope.idempotency_key,
        )
