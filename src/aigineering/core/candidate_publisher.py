"""Identity-neutral publication through the Candidate commitment boundary."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.core.commitment import CandidateCommitter, CommitmentDecision
from aigineering.core.signing import Signer
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    GenesisManifest,
    create_candidate_proposal,
)


def publish_effect(
    store,
    trace,
    genesis: GenesisManifest,
    actor_key: ActorKey,
    signer: Signer,
    effect: CandidateEffect,
    *,
    idempotency_key: str,
    causal_parents: tuple[str, ...] = (),
) -> CommitmentDecision:
    """Sign and commit one effect for an explicitly selected actor key."""
    return publish_effects(
        store,
        trace,
        genesis,
        actor_key,
        signer,
        (effect,),
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
    )


def publish_effects(
    store,
    trace,
    genesis: GenesisManifest,
    actor_key: ActorKey,
    signer: Signer,
    effects: tuple[CandidateEffect, ...],
    *,
    idempotency_key: str,
    causal_parents: tuple[str, ...] = (),
) -> CommitmentDecision:
    """Sign and atomically commit one Candidate effect batch."""
    if actor_key.public_key != signer.signer_id or actor_key.kind != signer.kind:
        raise ValueError("publisher signer does not match the selected actor key")
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        effects=effects,
        signer=signer,
        causal_parents=causal_parents,
        idempotency_key=idempotency_key,
    )
    return CandidateCommitter(store, trace).commit(candidate, genesis)


@dataclass(frozen=True)
class CandidatePublisher:
    """Explicit actor-bound publisher injectable into plugins and adapters."""

    store: object
    trace: object
    genesis: GenesisManifest
    actor_key: ActorKey
    signer: Signer

    def publish(
        self,
        effects: tuple[CandidateEffect, ...],
        *,
        idempotency_key: str,
        causal_parents: tuple[str, ...] = (),
    ) -> CommitmentDecision:
        return publish_effects(
            self.store,
            self.trace,
            self.genesis,
            self.actor_key,
            self.signer,
            effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )
