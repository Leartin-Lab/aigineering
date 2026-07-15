"""Local actor key persistence shared by Candidate-aware CLI commands."""

from __future__ import annotations

import os
from pathlib import Path

from aigineering.core.domain import initialize_genesis, load_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.candidate import (
    ActorKey,
    GenesisManifest,
    create_genesis_manifest,
)


LOCAL_ROOT_CAPABILITIES = (
    "asset.publish",
    "asset.publish.protected",
    "asset.relate",
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
