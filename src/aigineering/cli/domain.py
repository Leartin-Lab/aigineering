"""Initialize and inspect the local Candidate trust domain."""

from __future__ import annotations

import click
import sqlite3

from aigineering.cli._common import (
    _output_json,
    _persistent_store,
    _persistent_snapshot,
)
from aigineering.runtime_services import sync_runtime
from aigineering.local_domain import initialize_local_domain
from aigineering.core.domain import load_genesis
from aigineering.runtime_upgrade import upgrade_local_store


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
    try:
        try:
            initialized = initialize_local_domain(
                store, domain_name=domain_name, actor_id=actor_id, key_id=key_id
            )
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        sync_runtime(store, accepted=True)
    finally:
        store.close()

    result = {
        "actor_id": initialized.actor_id,
        "domain_id": initialized.manifest.id,
        "key_file": str(initialized.key_path),
        "key_id": initialized.key_id,
        "public_key": initialized.signer.signer_id,
    }
    if as_json:
        _output_json(result)
    else:
        click.echo(f"Domain initialized: {initialized.manifest.id}")
        click.echo(f"Actor key: {initialized.key_path}")


@domain_group.command("show")
@click.option("--json", "as_json", is_flag=True)
def domain_show(as_json: bool) -> None:
    """Show public Genesis identity; never print private key material."""
    store = _persistent_snapshot()
    try:
        manifest = load_genesis(store)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()
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


@domain_group.command("upgrade")
@click.option("--json", "as_json", is_flag=True)
def domain_upgrade(as_json: bool) -> None:
    """Back up and migrate an existing local SQLite Store schema."""
    try:
        result = upgrade_local_store()
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        message = str(exc)
        if as_json:
            _output_json(
                {"ok": False, "error": {"code": "upgrade_failed", "message": message}}
            )
            raise click.exceptions.Exit(1) from exc
        raise click.ClickException(message) from exc

    if as_json:
        _output_json({"ok": True, "data": result})
        return
    click.echo(
        f"SQLite schema upgraded from {result['from_version']} to {result['to_version']}"
    )
    backup = result["backup"]
    click.echo(
        f"Backup: {backup if backup is not None else '(not needed; already current)'}"
    )
    click.echo(f"Runtime revision: {result['revision']}")
