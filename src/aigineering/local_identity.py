"""Local actor-key persistence for the reference runtime composition."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from dataclasses import replace

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.domain import initialize_genesis, load_genesis
from aigineering.core.ids import compute_content_hash
from aigineering.core.signing import Ed25519Signer
from aigineering.agent.worker import WorkerHost
from aigineering.protocol.candidate import (
    ActorKey,
    GenesisManifest,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
)
from aigineering.core.worker_routing import (
    WorkerRegistration,
    worker_registration_payload,
)
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.plugins.worker_profile_promotion import (
    WORKER_PROFILE_PROMOTION_EFFECTS,
    completed_profile_promotion_matches,
    validate_worker_profile_promotion,
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


class _ActorKeyAlreadyExists(ValueError):
    """The requested actor key path was published by another creator."""


def actor_key_path() -> Path:
    return Path(os.environ.get("AIG_ACTOR_KEY_FILE", ".aig/identity/root.ed25519"))


def write_actor_key(path: Path, signer: Ed25519Signer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(signer.private_key_hex + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError as exc:
            raise _ActorKeyAlreadyExists(f"actor key already exists at {path}") from exc
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


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
    try:
        write_actor_key(path, signer)
    except _ActorKeyAlreadyExists:
        # Another local Fleet process may have won the atomic publication.
        return load_actor_signer(path)
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


def _local_root_publisher(
    store, *, additional_effects=None
) -> tuple[GenesisManifest, CandidatePublisher]:
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
    return genesis, CandidatePublisher(
        store,
        store,
        genesis,
        actor_key,
        signer,
        additional_effects=additional_effects,
    )


def ensure_local_worker_host(
    store,
    worker,
    *,
    effect_capabilities: tuple[str, ...] = (),
    pull_only: bool = False,
):
    """Bind one local Worker adapter to its immutable registration history."""
    from aigineering.application import require_redis

    require_redis()
    genesis, authority = _local_root_publisher(
        store, additional_effects=WORKER_PROFILE_PROMOTION_EFFECTS
    )
    worker_id = str(worker.worker_id)
    effect_capabilities = tuple(sorted(set(effect_capabilities)))
    key_identity = (
        worker_id
        if not effect_capabilities
        else f"{worker_id}:{','.join(effect_capabilities)}"
    )
    key_suffix = compute_content_hash(key_identity)[:16]
    key_id = f"worker-{key_suffix}"
    path = actor_key_path().parent / f"{key_id}.ed25519"
    current = store.get_worker_registration(worker_id)
    registration_factory = getattr(worker, "registration", None)
    requested = (
        registration_factory()
        if callable(registration_factory)
        else WorkerRegistration(worker_id)
    )

    if current is None:
        effective = load_effective_actor_keys(store, genesis)
        authorized = next(
            (
                key
                for key in effective
                if key.actor_id == worker_id and key.key_id == key_id
            ),
            None,
        )
        if authorized is not None and not path.is_file():
            raise ValueError(
                "authorized local Worker key has no local signer; refusing to create a replacement"
            )
        worker_signer = _load_or_create_signer(path)
        actor_key = ActorKey(
            worker_id,
            key_id,
            worker_signer.kind,
            worker_signer.signer_id,
            ("worker.submit", *effect_capabilities),
        )
        return authorize_worker_host(
            worker, genesis, actor_key, worker_signer, authority
        )

    # Existing registrations must prove their local signer before any new key
    # can be written. This also refuses disabled and externally owned workers.
    existing_host = load_existing_local_worker_host(
        store, worker, effect_capabilities=effect_capabilities
    )
    requested = replace(
        requested,
        worker_id=worker_id,
        actor_id=existing_host.actor_key.actor_id,
        key_id=existing_host.actor_key.key_id,
    )
    if worker_registration_payload(current) == worker_registration_payload(requested):
        return replace(existing_host, registration_version=current.version)

    records = tuple(record for _, record in store.scan_runtime_records())
    if pull_only:
        pull_view = replace(
            requested, profile_id=current.profile_id, version=current.version
        )
        if worker_registration_payload(current) == worker_registration_payload(
            pull_view
        ):
            return replace(existing_host, registration_version=current.version)
        raise ValueError(
            f"Worker {worker_id!r} registration version {current.version!r} has "
            "routing metadata incompatible with pull-only package retrieval"
        )
    if (
        requested.version == "1"
        and current.version != "1"
        and completed_profile_promotion_matches(
            records, requested=requested, current=current
        )
    ):
        return replace(existing_host, registration_version=current.version)

    actor_keys = tuple(load_effective_actor_keys(store, genesis))
    context = load_effect_projection_context(store, actor_keys=actor_keys)
    history = [
        record
        for record in records
        if record.record_type == "worker.registered"
        and record.payload.get("worker_id") == worker_id
    ]
    if (
        requested.version == "1"
        and current.version.isdecimal()
        and str(int(current.version)) == current.version
        and len(history) == 1
        and requested.profile_id
        and not effect_capabilities
    ):
        promoted = replace(requested, version=str(int(current.version) + 1))
        previous = validate_worker_profile_promotion(
            context,
            previous_registration_id=history[0].id,
            registration=promoted,
        )
        return authorize_worker_host(
            worker,
            genesis,
            existing_host.actor_key,
            existing_host.signer,
            authority,
            registration=promoted,
            promotion_previous_registration_id=previous.id,
        )

    changed = [
        name
        for name, value in worker_registration_payload(requested).items()
        if value != worker_registration_payload(current).get(name)
    ]
    raise ValueError(
        f"Worker {worker_id!r} registration version {current.version!r} does not "
        f"match this adapter (changed fields: {', '.join(changed) or 'version'}); "
        "use an explicitly authorized new worker registration version"
    )


def load_existing_local_worker_host(
    store, worker, *, effect_capabilities: tuple[str, ...] = ()
):
    """Load an already registered local Worker signer without provisioning."""
    genesis = load_genesis(store)
    registration = store.get_worker_registration(str(worker.worker_id))
    if registration is None or not registration.enabled:
        raise ValueError("no enabled local Worker registration exists")
    worker_id = str(worker.worker_id)
    effect_capabilities = tuple(sorted(set(effect_capabilities)))
    key_identity = (
        worker_id
        if not effect_capabilities
        else f"{worker_id}:{','.join(effect_capabilities)}"
    )
    key_id = f"worker-{compute_content_hash(key_identity)[:16]}"
    if registration.actor_id != worker_id or registration.key_id != key_id:
        raise ValueError(
            "registered Worker identity is not the canonical local signer; "
            "provide a signed Worker Candidate"
        )
    path = actor_key_path().parent / f"{key_id}.ed25519"
    if not path.is_file():
        raise ValueError(
            "registered Worker signer is not available locally; "
            "provide a signed Worker Candidate"
        )
    signer = load_actor_signer(path)
    actor_key = next(
        (
            key
            for key in load_effective_actor_keys(store, genesis)
            if key.actor_id == worker_id and key.key_id == key_id
        ),
        None,
    )
    if (
        actor_key is None
        or actor_key.revoked
        or actor_key.public_key != signer.signer_id
        or "worker.submit" not in actor_key.capabilities
    ):
        raise ValueError(
            "local Worker signer does not match current authorization; "
            "provide a signed Worker Candidate"
        )
    return WorkerHost(
        worker, genesis, actor_key, signer, registration_version=registration.version
    )


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
