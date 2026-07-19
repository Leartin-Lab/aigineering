"""Authentication boundary for operational Worker coordination Candidates."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.protocol.candidate import (
    CandidateProposal,
    candidate_received_record,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record


@dataclass(frozen=True)
class AuthenticatedWorkerCommand:
    """One signed, actor-bound, single-use coordination request."""

    payload: Mapping[str, Any]
    runtime_records: tuple[RuntimeRecord, ...]


def authenticate_worker_command(
    candidate: CandidateProposal,
    effect_type: str,
    store,
) -> AuthenticatedWorkerCommand:
    """Verify a claim/renew command against current actor and routing facts."""
    if not candidate.idempotency_key:
        raise ValueError("worker command requires a non-empty idempotency_key")
    if len(candidate.effects) != 1 or candidate.effects[0].effect_type != effect_type:
        raise ValueError(f"worker command requires exactly one {effect_type} effect")
    genesis = load_genesis(store)
    actor_keys = load_effective_actor_keys(store, genesis)
    receipt = candidate_received_record(candidate, genesis, actor_keys=actor_keys)
    key = next(
        item
        for item in actor_keys
        if item.actor_id == candidate.actor_id and item.key_id == candidate.key_id
    )
    if "worker.submit" not in key.capabilities:
        raise ValueError("worker command actor lacks worker.submit capability")
    payload = deep_thaw(candidate.effects[0].payload)
    worker_id = str(payload.get("worker_id", ""))
    registration = store.get_worker_registration(worker_id)
    if (
        not worker_id
        or candidate.actor_id != worker_id
        or registration is None
        or not registration.enabled
        or registration.actor_id != candidate.actor_id
        or registration.key_id != candidate.key_id
    ):
        raise ValueError(
            "worker command does not match an enabled actor-key registration"
        )
    command = create_runtime_record(
        f"{effect_type}.requested",
        {
            "candidate_id": candidate.id,
            "idempotency_key": candidate.idempotency_key,
            "key_id": candidate.key_id,
            **payload,
        },
        causal_parents=(receipt.id,),
    )
    return AuthenticatedWorkerCommand(
        payload=MappingProxyType(payload),
        runtime_records=(receipt, command),
    )
