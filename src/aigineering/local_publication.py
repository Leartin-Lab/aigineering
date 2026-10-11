"""Shared local Candidate publication helpers for application adapters."""

from __future__ import annotations

from aigineering.core.candidate_publisher import publish_effects
from aigineering.core.commitment import CommitmentDecision
from aigineering.core.domain import load_genesis
from aigineering.local_identity import load_actor_signer
from aigineering.protocol.candidate import CandidateEffect
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    runtime_record_effective_payload,
)
from aigineering.application import require_redis
from aigineering.runtime_services import RedisSyncPending, sync_runtime


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
        (
            record
            for record in decision.runtime_records
            if record.record_type.endswith("rejected")
        ),
        None,
    )
    if rejection is None:
        raise ValueError("Candidate was not accepted and produced no rejection record")
    raise ValueError(str(rejection.payload.get("reason", "Candidate was rejected")))


def verify_durable_candidate_member(
    store, decision, member: RuntimeRecord
) -> RuntimeRecord:
    """Verify one returned Candidate output against its exact receipt and head.

    ``decision`` is only the in-memory result of this publication call. This
    helper never uses it to grant authority: it requires the member to be in
    that Candidate's committed head and equal to the durable SQLite record.
    """
    if not isinstance(decision, CommitmentDecision) or not decision.accepted:
        raise ValueError("Candidate decision did not accept an output")
    receipts = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == decision.candidate_id
    )
    heads = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == decision.candidate_id
    )
    if len(receipts) != 1 or len(heads) != 1:
        raise RuntimeError("accepted Candidate result has no unique receipt and head")
    receipt, head = receipts[0], heads[0]
    committed_ids = tuple(head.payload.get("committed_record_ids", ()))
    if (
        receipt.id not in head.causal_parents
        or member.id not in committed_ids
        or member.id not in head.causal_parents
    ):
        raise RuntimeError(
            "Candidate output is not a member of its exact committed head"
        )
    for expected in (receipt, member, head):
        durable = store.get_runtime_record(expected.id)
        if (
            durable is None
            or durable.id != expected.id
            or durable.record_type != expected.record_type
            or durable.schema_version != expected.schema_version
            or runtime_record_effective_payload(durable)
            != runtime_record_effective_payload(expected)
        ):
            raise RuntimeError("accepted Candidate result is not durably identical")
    return store.get_runtime_record(member.id)


def verify_durable_rejection_member(store, decision) -> RuntimeRecord:
    """Return the exact durable rejection fact from this non-accepted decision.

    Authentication rejections may only carry claimed IDs; this helper treats
    them as rejection metadata and never as a signed receipt or author proof.
    """
    if not isinstance(decision, CommitmentDecision) or decision.accepted:
        raise ValueError("Candidate decision is not a rejection")
    matches = tuple(
        record
        for record in decision.runtime_records
        if record.record_type
        in {
            "candidate.rejected",
            "candidate.authentication_rejected",
        }
    )
    if len(matches) != 1:
        raise RuntimeError("rejected Candidate decision has no unique rejection fact")
    expected = matches[0]
    durable = store.get_runtime_record(expected.id)
    if (
        durable is None
        or durable.id != expected.id
        or durable.record_type != expected.record_type
        or durable.schema_version != expected.schema_version
        or runtime_record_effective_payload(durable)
        != runtime_record_effective_payload(expected)
    ):
        raise RuntimeError("Candidate rejection is not durably identical")
    return durable


def commit_local_effects(
    store,
    effects: tuple[CandidateEffect, ...],
    *,
    idempotency_key: str,
    causal_parents: tuple[str, ...] = (),
) -> CommitmentDecision:
    require_redis()
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
    decision = publish_effects(
        store,
        store,
        genesis,
        actor_key,
        signer,
        effects,
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
    )
    try:
        sync_runtime(store, accepted=decision.accepted)
    except RedisSyncPending as exc:
        exc.decision = decision
        exc.revision = store.get_runtime_revision()
        raise
    return decision
