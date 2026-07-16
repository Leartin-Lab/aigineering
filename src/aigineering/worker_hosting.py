"""Identity-neutral WorkerHost authorization and registration."""

from __future__ import annotations

from dataclasses import replace

from aigineering.agent.worker import Worker, WorkerHost
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.core.signing import Signer
from aigineering.core.worker_routing import (
    WorkerRegistration,
    worker_registration_payload,
)
from aigineering.protocol.candidate import ActorKey, GenesisManifest
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    worker_registration_effect,
)


def authorize_worker_host(
    worker: Worker,
    genesis: GenesisManifest,
    actor_key: ActorKey,
    signer: Signer,
    authority: CandidatePublisher,
) -> WorkerHost:
    """Bind *worker* after publishing any missing identity facts."""
    if authority.genesis.id != genesis.id:
        raise ValueError("worker authority and WorkerHost must share one domain")
    current_key = next(
        (
            key
            for key in load_effective_actor_keys(authority.store, genesis)
            if (key.actor_id, key.key_id) == (actor_key.actor_id, actor_key.key_id)
        ),
        None,
    )
    if current_key is not None and current_key != actor_key:
        raise ValueError(
            f"worker key {actor_key.actor_id}/{actor_key.key_id} cannot be rebound"
        )

    registration_factory = getattr(worker, "registration", None)
    registration = (
        registration_factory()
        if callable(registration_factory)
        else WorkerRegistration(worker.worker_id)
    )
    registration = replace(
        registration,
        worker_id=worker.worker_id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
    )
    effects = []
    if current_key is None:
        effects.append(actor_authorization_effect(actor_key))
    if authority.store.get_worker_registration(worker.worker_id) != registration:
        effects.append(worker_registration_effect(registration))
    if effects:
        identity = compute_content_hash(
            canonical_json(worker_registration_payload(registration))
        )[:16]
        decision = authority.publish(
            tuple(effects),
            idempotency_key=f"worker-host:{worker.worker_id}:{identity}",
        )
        if not decision.accepted:
            rejection = next(
                (
                    record
                    for record in decision.runtime_records
                    if record.record_type.endswith("rejected")
                ),
                None,
            )
            reason = rejection.payload["reason"] if rejection is not None else "unknown"
            raise ValueError(
                f"worker {worker.worker_id!r} was not authorized: {reason}"
            )
    return WorkerHost(worker, genesis, actor_key, signer)
