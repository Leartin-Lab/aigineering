"""Local actor key persistence shared by Candidate-aware CLI commands."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from aigineering.agent.worker import WorkerHost
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_publisher import CandidatePublisher, publish_effects
from aigineering.core.domain import initialize_genesis, load_genesis
from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.core.signing import Ed25519Signer
from aigineering.core.worker_routing import (
    WorkerRegistration,
    worker_registration_payload,
)
from aigineering.protocol.candidate import (
    ActorKey,
    GenesisManifest,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    worker_registration_effect,
)


LOCAL_ROOT_CAPABILITIES = (
    "asset.publish",
    "asset.publish.protected",
    "asset.relate",
    "actor.authorize",
    "actor.revoke",
    "actor.rotate",
    "contract.publish",
    "contract.cancel",
    "worker.register",
)


def actor_key_path() -> Path:
    return Path(os.environ.get("AIG_ACTOR_KEY_FILE", ".aig/identity/root.ed25519"))


def write_actor_key(path: Path, signer: Ed25519Signer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"actor key already exists at {path}") from exc
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(signer.private_key_hex + "\n")


def load_actor_signer(path: Path | None = None) -> Ed25519Signer:
    selected = path or actor_key_path()
    try:
        mode = selected.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError(
                f"actor key {selected} permissions are too broad; require mode 0600"
            )
        encoded = selected.read_text(encoding="ascii").strip()
    except FileNotFoundError as exc:
        raise ValueError(
            f"actor key not found at {selected}; run 'aig domain init'"
        ) from exc
    return Ed25519Signer.from_private_key_hex(encoded)


def ensure_local_domain(store) -> GenesisManifest:
    """Create the sole local bootstrap on first use, otherwise load it."""
    try:
        return load_genesis(store)
    except LookupError:
        path = actor_key_path()
        if path.exists():
            signer = load_actor_signer(path)
        else:
            signer = Ed25519Signer()
            write_actor_key(path, signer)
        manifest = create_genesis_manifest(
            "local",
            [
                ActorKey(
                    "human:owner",
                    "root-1",
                    signer.kind,
                    signer.signer_id,
                    LOCAL_ROOT_CAPABILITIES,
                )
            ],
            "policy:bootstrap-v1",
        )
        return initialize_genesis(store, manifest)


def ensure_local_worker_host(store, worker) -> WorkerHost:
    """Bind a local execution adapter to one durable delegated actor key."""
    genesis = ensure_local_domain(store)
    root_signer = load_actor_signer()
    try:
        root_key = next(
            key
            for key in genesis.root_keys
            if key.public_key == root_signer.signer_id and not key.revoked
        )
    except StopIteration as exc:
        raise ValueError("local root signer is not authorized by Genesis") from exc

    worker_id = str(worker.worker_id)
    key_suffix = compute_content_hash(worker_id)[:16]
    key_id = f"worker-{key_suffix}"
    path = actor_key_path().parent / f"{key_id}.ed25519"
    if path.exists():
        worker_signer = load_actor_signer(path)
    else:
        worker_signer = Ed25519Signer()
        write_actor_key(path, worker_signer)
    actor_key = ActorKey(
        worker_id,
        key_id,
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )

    effective = load_effective_actor_keys(store, genesis)
    current_key = next(
        (
            key
            for key in effective
            if key.actor_id == worker_id and key.key_id == key_id
        ),
        None,
    )
    if current_key is not None and current_key != actor_key:
        raise ValueError(f"local worker key {worker_id}/{key_id} cannot be rebound")

    registration_factory = getattr(worker, "registration", None)
    registration = (
        registration_factory()
        if callable(registration_factory)
        else WorkerRegistration(worker_id)
    )
    registration = replace(
        registration,
        worker_id=worker_id,
        actor_id=worker_id,
        key_id=key_id,
    )
    effects = []
    if current_key is None:
        effects.append(actor_authorization_effect(actor_key))
    if store.get_worker_registration(worker_id) != registration:
        effects.append(worker_registration_effect(registration))
    if effects:
        identity = compute_content_hash(
            canonical_json(worker_registration_payload(registration))
        )[:16]
        decision = publish_effects(
            store,
            store,
            genesis,
            root_key,
            root_signer,
            tuple(effects),
            idempotency_key=f"worker-host:{worker_id}:{identity}",
        )
        if not decision.accepted:
            rejection = next(
                record
                for record in decision.runtime_records
                if record.record_type.endswith("rejected")
            )
            raise ValueError(str(rejection.payload["reason"]))
    return WorkerHost(worker, genesis, actor_key, worker_signer)


def ensure_local_plugin_publisher(
    store,
    plugin_id: str,
    capabilities: tuple[str, ...],
) -> CandidatePublisher:
    """Provision one durable plugin actor and return its explicit publisher."""
    genesis = ensure_local_domain(store)
    root_signer = load_actor_signer()
    root_key = next(
        key
        for key in genesis.root_keys
        if key.public_key == root_signer.signer_id and not key.revoked
    )
    actor_id = f"plugin:{plugin_id}"
    key_suffix = compute_content_hash(actor_id)[:16]
    key_id = f"plugin-{key_suffix}"
    path = actor_key_path().parent / f"{key_id}.ed25519"
    if path.exists():
        signer = load_actor_signer(path)
    else:
        signer = Ed25519Signer()
        write_actor_key(path, signer)
    actor_key = ActorKey(
        actor_id,
        key_id,
        signer.kind,
        signer.signer_id,
        capabilities,
    )
    current = next(
        (
            key
            for key in load_effective_actor_keys(store, genesis)
            if key.actor_id == actor_id and key.key_id == key_id
        ),
        None,
    )
    if current is None:
        decision = publish_effects(
            store,
            store,
            genesis,
            root_key,
            root_signer,
            (actor_authorization_effect(actor_key),),
            idempotency_key=f"plugin-key:{actor_id}:{key_id}",
        )
        if not decision.accepted:
            raise ValueError(f"plugin actor {actor_id!r} could not be authorized")
    elif current != actor_key:
        raise ValueError(f"plugin key {actor_id}/{key_id} cannot be rebound")
    return CandidatePublisher(store, store, genesis, actor_key, signer)
