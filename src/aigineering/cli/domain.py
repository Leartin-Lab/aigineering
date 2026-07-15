"""Initialize and inspect the local Candidate trust domain."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.cli.identity import (
    LOCAL_ROOT_CAPABILITIES,
    actor_key_path,
    write_actor_key,
)
from aigineering.core.domain import initialize_genesis, load_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest


@click.group("domain")
def domain_group() -> None:
    """Initialize and inspect the signed Candidate domain."""


@domain_group.command("init")
@click.option("--domain", "domain_name", default="local", show_default=True)
@click.option("--actor", "actor_id", default="human:owner", show_default=True)
@click.option("--key-id", default="root-1", show_default=True)
@click.option("--json", "as_json", is_flag=True)
def domain_init(domain_name: str, actor_id: str, key_id: str, as_json: bool) -> None:
    """Create one Ed25519 root key and immutable Genesis record."""
    store = _persistent_store()
    path = actor_key_path()
    try:
        load_genesis(store)
    except LookupError:
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
        try:
            write_actor_key(path, signer)
            initialize_genesis(store, manifest)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        raise click.ClickException("runtime domain is already initialized")

    result = {
        "actor_id": actor_id,
        "domain_id": manifest.id,
        "key_file": str(path),
        "key_id": key_id,
        "public_key": signer.signer_id,
    }
    if as_json:
        _output_json(result)
    else:
        click.echo(f"Domain initialized: {manifest.id}")
        click.echo(f"Actor key: {path}")


@domain_group.command("show")
@click.option("--json", "as_json", is_flag=True)
def domain_show(as_json: bool) -> None:
    """Show public Genesis identity; never print private key material."""
    store = _persistent_store()
    try:
        manifest = load_genesis(store)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    result = {
        "domain": manifest.domain,
        "domain_id": manifest.id,
        "policy_hash": manifest.policy_hash,
        "root_keys": [
            {
                "actor_id": key.actor_id,
                "capabilities": list(key.capabilities),
                "key_id": key.key_id,
                "kind": key.kind,
                "public_key": key.public_key,
                "revoked": key.revoked,
            }
            for key in manifest.root_keys
        ],
    }
    if as_json:
        _output_json(result)
    else:
        click.echo(f"Domain: {manifest.domain} ({manifest.id})")
        for key in manifest.root_keys:
            click.echo(f"Root actor: {key.actor_id} [{key.key_id}] {key.kind}")
