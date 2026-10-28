"""Shared explicit bootstrap for one local Candidate domain."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aigineering.core.domain import initialize_genesis, load_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.local_identity import (
    LOCAL_ROOT_CAPABILITIES,
    actor_key_path,
    write_actor_key,
)
from aigineering.protocol.candidate import (
    ActorKey,
    GenesisManifest,
    create_genesis_manifest,
)


@dataclass(frozen=True)
class LocalDomainInitialization:
    manifest: GenesisManifest
    signer: Ed25519Signer
    key_path: Path
    actor_id: str
    key_id: str


def initialize_local_domain(
    store,
    *,
    domain_name: str = "local",
    actor_id: str = "human:owner",
    key_id: str = "root-1",
    key_path: Path | None = None,
) -> LocalDomainInitialization:
    """Create a new local Genesis and its root key exactly once.

    A key left by a failed initialization is deliberately retained and blocks
    silent replacement. Callers can distinguish that partial setup and choose
    an explicit recovery path.
    """
    try:
        load_genesis(store)
    except LookupError:
        pass
    else:
        raise ValueError("runtime domain is already initialized")

    selected_path = key_path or actor_key_path()
    if selected_path.exists():
        raise ValueError(
            f"actor key already exists at {selected_path}; refusing to replace it"
        )
    signer = Ed25519Signer()
    manifest = create_genesis_manifest(
        domain_name,
        [
            ActorKey(
                actor_id,
                key_id,
                signer.kind,
                signer.signer_id,
                LOCAL_ROOT_CAPABILITIES,
            )
        ],
        "policy:bootstrap-v1",
    )
    write_actor_key(selected_path, signer)
    initialized = initialize_genesis(store, manifest)
    return LocalDomainInitialization(
        initialized, signer, selected_path, actor_id, key_id
    )
