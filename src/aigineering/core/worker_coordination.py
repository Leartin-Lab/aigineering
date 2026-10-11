"""Authentication boundary for operational Worker coordination Candidates."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateProposal,
    GenesisManifest,
    candidate_received_record,
    create_candidate_proposal,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.signing import Signer


_CLAIM_FIELDS = frozenset({"worker_id", "contract_id", "lease_seconds"})
_RENEW_FIELDS = frozenset({"worker_id", "claim_id", "claim_epoch", "lease_seconds"})


def same_worker_command_records(
    supplied: tuple[RuntimeRecord, ...], canonical: tuple[RuntimeRecord, ...]
) -> bool:
    """Compare canonical command facts while ignoring observation timestamps."""
    from aigineering.protocol.runtime_record import runtime_record_effective_payload

    return tuple(
        (item.id, runtime_record_effective_payload(item)) for item in supplied
    ) == tuple((item.id, runtime_record_effective_payload(item)) for item in canonical)


@dataclass(frozen=True)
class AuthenticatedWorkerCommand:
    """One signed, actor-bound, single-use coordination request."""

    candidate: CandidateProposal
    payload: Mapping[str, Any]
    runtime_records: tuple[RuntimeRecord, ...]


def compile_worker_command(
    domain_id: str,
    actor_key: ActorKey,
    signer: Signer,
    effect,
    *,
    idempotency_key: str,
) -> CandidateProposal:
    """Compile one Worker coordination effect with its actual authorized key."""
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("worker command requires a non-empty idempotency_key")
    if signer.kind != actor_key.kind or signer.signer_id != actor_key.public_key:
        raise ValueError("worker signer does not match its authorized actor key")
    if "worker.submit" not in actor_key.capabilities:
        raise ValueError("worker actor key requires worker.submit capability")
    if effect.effect_type not in {"worker.claim", "worker.claim.renew"}:
        raise ValueError("unsupported Worker coordination effect")
    return create_candidate_proposal(
        domain_id=domain_id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        effects=(effect,),
        signer=signer,
        idempotency_key=idempotency_key,
    )


def _strict_payload(candidate: CandidateProposal, effect_type: str) -> dict[str, Any]:
    if not candidate.idempotency_key or not candidate.idempotency_key.strip():
        raise ValueError("worker command requires a non-empty idempotency_key")
    if len(candidate.effects) != 1 or candidate.effects[0].effect_type != effect_type:
        raise ValueError(f"worker command requires exactly one {effect_type} effect")
    payload = deep_thaw(candidate.effects[0].payload)
    if not isinstance(payload, dict):
        raise ValueError(f"{effect_type} payload must be an object")
    required = _CLAIM_FIELDS if effect_type == "worker.claim" else _RENEW_FIELDS
    if set(payload) != required:
        missing = sorted(required - set(payload))
        unknown = sorted(set(payload) - required)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise ValueError(
            f"{effect_type} payload has invalid fields: {'; '.join(detail)}"
        )
    worker_id = payload["worker_id"]
    lease_seconds = payload["lease_seconds"]
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError(f"{effect_type} worker_id must be a non-empty string")
    if type(lease_seconds) is not int or lease_seconds < 1:
        raise ValueError(f"{effect_type} lease_seconds must be a positive integer")
    if effect_type == "worker.claim":
        contract_id = payload["contract_id"]
        if contract_id is not None and (
            not isinstance(contract_id, str) or not contract_id.strip()
        ):
            raise ValueError(
                "worker.claim contract_id must be a non-empty string or null"
            )
    else:
        claim_id = payload["claim_id"]
        claim_epoch = payload["claim_epoch"]
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValueError("worker.claim.renew claim_id must be a non-empty string")
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError(
                "worker.claim.renew claim_epoch must be a positive integer"
            )
    return payload


def parse_worker_command_payload(
    candidate: CandidateProposal, effect_type: str
) -> Mapping[str, Any]:
    """Parse exact signed command shape without granting any authority."""
    return MappingProxyType(_strict_payload(candidate, effect_type))


def _validate_command_identity(
    candidate: CandidateProposal,
    effect_type: str,
    payload: Mapping[str, Any],
    *,
    genesis: GenesisManifest,
    actor_keys: tuple[ActorKey, ...],
    registration: WorkerRegistration | None,
) -> tuple[RuntimeRecord, RuntimeRecord]:
    receipt = candidate_received_record(candidate, genesis, actor_keys=actor_keys)
    worker_id = payload["worker_id"]
    if candidate.actor_id != worker_id:
        raise ValueError("worker command actor does not match worker_id")
    key = next(
        (
            item
            for item in actor_keys
            if item.actor_id == candidate.actor_id and item.key_id == candidate.key_id
        ),
        None,
    )
    if key is None or key.revoked:
        raise ValueError("worker command key is not currently authorized")
    if "worker.submit" not in key.capabilities:
        raise ValueError("worker command actor lacks worker.submit capability")
    if (
        registration is None
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
    return receipt, command


def _validate_current_command(
    candidate: CandidateProposal,
    effect_type: str,
    *,
    genesis: GenesisManifest,
    actor_keys: tuple[ActorKey, ...],
    registration: WorkerRegistration | None,
) -> AuthenticatedWorkerCommand:
    payload = _strict_payload(candidate, effect_type)
    records = _validate_command_identity(
        candidate,
        effect_type,
        payload,
        genesis=genesis,
        actor_keys=actor_keys,
        registration=registration,
    )
    return AuthenticatedWorkerCommand(
        candidate=candidate,
        payload=MappingProxyType(payload),
        runtime_records=records,
    )


def authenticate_worker_command(
    candidate: CandidateProposal,
    effect_type: str,
    store,
) -> AuthenticatedWorkerCommand:
    """Preflight a signed command; the writer must repeat validation atomically."""
    payload = _strict_payload(candidate, effect_type)
    genesis = load_genesis(store)
    actor_keys = load_effective_actor_keys(store, genesis)
    registration = store.get_worker_registration(payload["worker_id"])
    return _validate_current_command(
        candidate,
        effect_type,
        genesis=genesis,
        actor_keys=actor_keys,
        registration=registration,
    )


def validate_worker_claim_candidate(
    candidate: CandidateProposal,
    *,
    genesis: GenesisManifest,
    actor_keys: tuple[ActorKey, ...],
    registration: WorkerRegistration | None,
    worker_id: str,
    requested_contract_id: str,
    lease_seconds: int,
    expected_registration_version: str = "",
) -> tuple[RuntimeRecord, RuntimeRecord]:
    """Validate the exact current signed claim while the claim writer is locked."""
    command = _validate_current_command(
        candidate,
        "worker.claim",
        genesis=genesis,
        actor_keys=actor_keys,
        registration=registration,
    )
    payload = command.payload
    target = payload["contract_id"]
    if (
        payload["worker_id"] != worker_id
        or target not in (None, requested_contract_id)
        or payload["lease_seconds"] != lease_seconds
    ):
        raise ValueError("signed worker.claim does not match the claim request")
    if expected_registration_version and (
        registration is None or registration.version != expected_registration_version
    ):
        raise ValueError("worker registration changed after package selection")
    return command.runtime_records


def validate_worker_renewal_candidate(
    candidate: CandidateProposal,
    *,
    genesis: GenesisManifest,
    actor_keys: tuple[ActorKey, ...],
    registration: WorkerRegistration | None,
    claim_id: str,
    claim_epoch: int,
    worker_id: str,
    lease_seconds: int,
) -> tuple[RuntimeRecord, RuntimeRecord]:
    """Validate the exact signed lease renewal while its writer is locked."""
    command = _validate_current_command(
        candidate,
        "worker.claim.renew",
        genesis=genesis,
        actor_keys=actor_keys,
        registration=registration,
    )
    payload = command.payload
    if (
        payload["claim_id"] != claim_id
        or payload["claim_epoch"] != claim_epoch
        or payload["worker_id"] != worker_id
        or payload["lease_seconds"] != lease_seconds
    ):
        raise ValueError("signed worker.claim.renew does not match the renewal request")
    return command.runtime_records
