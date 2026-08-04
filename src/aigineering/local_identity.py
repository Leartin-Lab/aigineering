"""Local actor-key persistence for the reference runtime composition."""

from __future__ import annotations

import os
from pathlib import Path

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.domain import initialize_genesis, load_genesis
from aigineering.core.ids import compute_content_hash
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.candidate import (
    ActorKey,
    GenesisManifest,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
)
from aigineering.worker_hosting import authorize_worker_host


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


def _posix_private_modes_supported() -> bool:
    return os.name != "nt"


def load_actor_signer(path: Path | None = None) -> Ed25519Signer:
    selected = path or actor_key_path()
    try:
        if _posix_private_modes_supported():
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


def _load_or_create_signer(path: Path) -> Ed25519Signer:
    if path.exists():
        return load_actor_signer(path)
    signer = Ed25519Signer()
    write_actor_key(path, signer)
    return signer


def ensure_local_domain(store) -> GenesisManifest:
    """Create the sole local bootstrap on first use, otherwise load it."""
    try:
        return load_genesis(store)
    except LookupError:
        path = actor_key_path()
        signer = _load_or_create_signer(path)
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


def _local_root_publisher(store) -> tuple[GenesisManifest, CandidatePublisher]:
    genesis = ensure_local_domain(store)
    signer = load_actor_signer()
    try:
        actor_key = next(
            key
            for key in genesis.root_keys
            if key.public_key == signer.signer_id and not key.revoked
        )
    except StopIteration as exc:
        raise ValueError("local root signer is not authorized by Genesis") from exc
    return genesis, CandidatePublisher(store, store, genesis, actor_key, signer)


def ensure_local_worker_host(store, worker):
    """Bind a local execution adapter to one durable delegated actor key."""
    genesis, authority = _local_root_publisher(store)
    worker_id = str(worker.worker_id)
    key_suffix = compute_content_hash(worker_id)[:16]
    key_id = f"worker-{key_suffix}"
    path = actor_key_path().parent / f"{key_id}.ed25519"
    worker_signer = _load_or_create_signer(path)
    actor_key = ActorKey(
        worker_id,
        key_id,
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )

    return authorize_worker_host(worker, genesis, actor_key, worker_signer, authority)


def ensure_local_plugin_publisher(
    store,
    plugin_id: str,
    capabilities: tuple[str, ...],
) -> CandidatePublisher:
    """Provision one durable plugin actor and return its explicit publisher."""
    genesis, authority = _local_root_publisher(store)
    actor_id = f"plugin:{plugin_id}"
    key_suffix = compute_content_hash(actor_id)[:16]
    key_id = f"plugin-{key_suffix}"
    path = actor_key_path().parent / f"{key_id}.ed25519"
    signer = _load_or_create_signer(path)
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
        decision = authority.publish(
            (actor_authorization_effect(actor_key),),
            idempotency_key=f"plugin-key:{actor_id}:{key_id}",
        )
        if not decision.accepted:
            raise ValueError(f"plugin actor {actor_id!r} could not be authorized")
    elif current != actor_key:
        raise ValueError(f"plugin key {actor_id}/{key_id} cannot be rebound")
    return CandidatePublisher(store, store, genesis, actor_key, signer)


def ensure_local_runtime_publishers(store) -> CandidatePublisherRegistry:
    """Provision the fixed plugin actors used by local completion/recovery."""
    specifications = (
        ("planning.expand.v1", ("contract.publish",)),
        (
            "continuation.publish.v1",
            ("contract.publish", "contract.publish.protected"),
        ),
        ("fail.report.v1", ("asset.publish", "asset.publish.protected")),
        (
            "recovery.publish.v1",
            (
                "asset.publish",
                "asset.publish.protected",
                "contract.publish",
                "contract.publish.protected",
            ),
        ),
    )
    return CandidatePublisherRegistry(
        tuple(
            (
                plugin_id,
                ensure_local_plugin_publisher(store, plugin_id, capabilities),
            )
            for plugin_id, capabilities in specifications
        )
    )
