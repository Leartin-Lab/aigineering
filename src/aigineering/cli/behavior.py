"""CLI commands for behavior prompt asset injection and inspection.

Behavior assets are lightweight promptable instructions (not executable
skills).  They carry content (e.g. Markdown from a file), trust-tier
metadata, and are resolved via ``behavior:{name}`` labels.
"""

from __future__ import annotations

from pathlib import Path

import click

from aigineering.cli._common import _output_json, _persistent_store, _query_projection
from aigineering.cli._candidate import commit_local_effect, require_accepted
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.protocol.effect_builders import asset_proposal_effect


BEHAVIOR_PREFIX = "behavior:"


@click.group("behavior")
def behavior_group() -> None:
    """Manage behavior prompt assets (behaviour instructions)."""
    pass


@behavior_group.command("add")
@click.option(
    "--name", required=True, help="Behavior name (stored as behavior:<name>)."
)
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True),
    required=True,
    help="Path to a Markdown (or text) file with the behaviour content.",
)
@click.option(
    "--trust-tier",
    default="human",
    show_default=True,
    help="Trust tier for the behaviour asset.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def behavior_add(
    name: str,
    file_path: str,
    trust_tier: str,
    as_json: bool,
) -> None:
    """Add a behaviour prompt asset from a file.

    The asset is stored under the name ``behavior:<name>`` so that
    contracts labelled with ``behavior:<name>`` receive it during
    disclosure.
    """
    asset_name = f"{BEHAVIOR_PREFIX}{name}"
    content = Path(file_path).read_text()

    store = _persistent_store()
    try:
        proposal = build_control_plane_asset(
            name=asset_name,
            content=content,
            origin="human",
            trust_tier=trust_tier,
            source_uri=str(Path(file_path).resolve()),
            promptable=True,
            content_type="text",
        )
        decision = require_accepted(
            commit_local_effect(
                store,
                asset_proposal_effect(proposal),
                idempotency_key=f"asset:{proposal.id}",
            )
        )
        asset = decision.assets[0]
    except (LookupError, ValueError) as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        _output_json(
            {"id": asset.id, "name": asset.name, "trust_tier": asset.trust_tier}
        )
    else:
        click.echo(f"Behaviour asset injected: {asset.name} ({asset.id[:16]}...)")
        click.echo(f"  trust_tier: {asset.trust_tier}")
        click.echo(f"  source:     {file_path}")


@behavior_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def behavior_list(as_json: bool) -> None:
    """List all behaviour prompt assets in the store."""
    store = _persistent_store()
    all_assets = _query_projection(store).get_all_assets()
    behavior_assets = [a for a in all_assets if a.name.startswith(BEHAVIOR_PREFIX)]

    if as_json:
        result = [
            {
                "id": a.id,
                "name": a.name,
                "trust_tier": a.trust_tier,
                "origin": a.origin,
            }
            for a in behavior_assets
        ]
        _output_json(result)
    else:
        if not behavior_assets:
            click.echo("No behaviour assets found.")
            return
        for a in behavior_assets:
            display_name = a.name[len(BEHAVIOR_PREFIX) :]
            click.echo(f"{a.id[:20]:<22} {a.trust_tier:<12} {display_name}")


@behavior_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def behavior_show(name: str, as_json: bool) -> None:
    """Show a behaviour prompt asset by name.

    NAME may be given with or without the ``behavior:`` prefix.
    """
    if not name.startswith(BEHAVIOR_PREFIX):
        name = f"{BEHAVIOR_PREFIX}{name}"

    store = _persistent_store()
    matches = _query_projection(store).get_assets_by_name(name)
    if not matches:
        raise click.ClickException(f"No behaviour asset named '{name}'")

    asset = matches[0]
    if as_json:
        data = {
            "id": asset.id,
            "name": asset.name,
            "content": asset.content,
            "content_type": asset.content_type,
            "origin": asset.origin,
            "trust_tier": asset.trust_tier,
            "source_uri": asset.source_uri,
            "promptable": asset.promptable,
            "signed_by": asset.signed_by,
            "definition_hash": asset.definition_hash,
            "content_hash": asset.content_hash,
        }
        _output_json(data)
    else:
        click.echo(f"id:              {asset.id}")
        click.echo(f"name:            {asset.name}")
        click.echo(f"origin:          {asset.origin}")
        click.echo(f"trust_tier:      {asset.trust_tier}")
        click.echo(f"promptable:      {asset.promptable}")
        click.echo(f"definition_hash: {asset.definition_hash}")
        click.echo(f"content_hash:    {asset.content_hash}")
        click.echo(f"source_uri:      {asset.source_uri}")
        click.echo("--- content ---")
        click.echo(asset.content)
