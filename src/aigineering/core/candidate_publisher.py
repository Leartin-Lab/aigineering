"""Identity-neutral publication through the Candidate commitment boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigineering.core.commitment import CandidateCommitter, CommitmentDecision
from aigineering.core.signing import Signer
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    GenesisManifest,
    create_candidate_proposal,
)

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol


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

    store: StoreProtocol
    trace: TraceStoreProtocol
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


@dataclass(frozen=True)
class CandidatePublisherRegistry:
    """Immutable lookup of explicitly identified plugin publishers."""

    publishers: tuple[tuple[str, CandidatePublisher], ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(sorted(self.publishers, key=lambda item: item[0]))
        plugin_ids = tuple(plugin_id for plugin_id, _publisher in normalized)
        if any(not plugin_id for plugin_id in plugin_ids):
            raise ValueError("publisher plugin_id must not be empty")
        if len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("publisher registry contains duplicate plugin ids")
        object.__setattr__(self, "publishers", normalized)

    def get(self, plugin_id: str) -> CandidatePublisher | None:
        return next(
            (
                publisher
                for registered_id, publisher in self.publishers
                if registered_id == plugin_id
            ),
            None,
        )
