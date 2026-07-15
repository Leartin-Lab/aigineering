"""Shared local actor assembly for CLI Candidate publication."""

from __future__ import annotations

from aigineering.cli.identity import load_actor_signer
from aigineering.core.candidate_publisher import publish_effects
from aigineering.core.commitment import CommitmentDecision
from aigineering.core.domain import load_genesis
from aigineering.protocol.candidate import CandidateEffect


def commit_local_effect(
    store,
    effect: CandidateEffect,
    *,
    idempotency_key: str,
    causal_parents: tuple[str, ...] = (),
) -> CommitmentDecision:
    return commit_local_effects(
        store,
        (effect,),
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
    )


def require_accepted(decision: CommitmentDecision) -> CommitmentDecision:
    if decision.accepted:
        return decision
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type.endswith("rejected")
    )
    raise ValueError(str(rejection.payload["reason"]))


def commit_local_effects(
    store,
    effects: tuple[CandidateEffect, ...],
    *,
    idempotency_key: str,
    causal_parents: tuple[str, ...] = (),
) -> CommitmentDecision:
    genesis = load_genesis(store)
    signer = load_actor_signer()
    try:
        actor_key = next(
            key
            for key in genesis.root_keys
            if key.public_key == signer.signer_id and not key.revoked
        )
    except StopIteration as exc:
        raise ValueError("local actor key is not authorized by domain Genesis") from exc
    return publish_effects(
        store,
        store,
        genesis,
        actor_key,
        signer,
        effects,
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
    )
