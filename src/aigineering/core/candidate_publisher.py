"""Identity-neutral publication through the Candidate commitment boundary."""

from __future__ import annotations

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
) -> CommitmentDecision:
    """Sign and commit one effect for an explicitly selected actor key."""
    if actor_key.public_key != signer.signer_id or actor_key.kind != signer.kind:
        raise ValueError("publisher signer does not match the selected actor key")
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        effects=[effect],
        signer=signer,
        idempotency_key=idempotency_key,
    )
    return CandidateCommitter(store, trace).commit(candidate, genesis)
