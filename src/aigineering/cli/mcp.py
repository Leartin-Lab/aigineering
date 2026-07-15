"""CLI commands for MCP capability descriptor assets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.core.capability_descriptors import (
    create_mcp_descriptor,
    verify_descriptor,
)
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.trace import create_entry

MCP_PREFIX = "_mcp_"


@click.group("mcp")
def mcp_group() -> None:
    """Manage MCP capability descriptor assets."""
    pass


def _read_schema(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise click.UsageError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise click.UsageError(f"{path} must contain a JSON object")
    return data


@mcp_group.command("add")
@click.option("--name", required=True, help="MCP server descriptor name.")
@click.option("--source-uri", required=True, help="Public MCP source URI.")
@click.option(
    "--trust-tier",
    default="configured",
    show_default=True,
    help="Trust tier for the descriptor.",
)
@click.option("--tool-name", default="", help="Optional MCP tool name.")
@click.option(
    "--input-schema-json",
    type=click.Path(exists=True),
    default=None,
    help="JSON Schema file for tool input.",
)
@click.option(
    "--output-schema-json",
    type=click.Path(exists=True),
    default=None,
    help="JSON Schema file for tool output.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def mcp_add(
    name: str,
    source_uri: str,
    trust_tier: str,
    tool_name: str,
    input_schema_json: str | None,
    output_schema_json: str | None,
    as_json: bool,
) -> None:
    """Create a trusted MCP descriptor asset.

    Private transport credentials must not be placed in the descriptor. Store
    only public capability metadata and a source URI.
    """
    input_schema = _read_schema(input_schema_json)
    output_schema = _read_schema(output_schema_json)
    descriptor = create_mcp_descriptor(
        name=name,
        source_uri=source_uri,
        trust_tier=trust_tier,
        tool_name=tool_name,
        input_schema=input_schema,
        output_schema=output_schema,
    )
    if not verify_descriptor(descriptor, kind="mcp"):
        raise click.ClickException(
            "MCP descriptor failed trust gate; use trust_tier >= configured."
        )

    store = _persistent_store()
    ingress = RuntimeIngress(store, store)
    ingress.accept_asset(descriptor, source="mcp_capability", allow_protected=True)
    store.append(
        create_entry(
            contract_id="control_plane",
            event_type="asset_injected",
            parent_id=descriptor.id,
            relation_type="mcp_capability",
            relation_target=descriptor.name,
            accepted_fragments=[
                json.dumps(
                    {
                        "asset_id": descriptor.id,
                        "origin": descriptor.origin,
                        "trust_tier": descriptor.trust_tier,
                    },
                    sort_keys=True,
                )
            ],
        )
    )

    if as_json:
        _output_json(
            {
                "id": descriptor.id,
                "name": descriptor.name,
                "trust_tier": descriptor.trust_tier,
                "source_uri": descriptor.source_uri,
            }
        )
    else:
        click.echo(
            f"MCP descriptor injected: {descriptor.name} ({descriptor.id[:16]}...)"
        )
        click.echo(f"  trust_tier: {descriptor.trust_tier}")
        click.echo(f"  source_uri: {descriptor.source_uri}")


@mcp_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def mcp_list(as_json: bool) -> None:
    """List MCP descriptor assets."""
    store = _persistent_store()
    descriptors = [a for a in store.get_all_assets() if a.name.startswith(MCP_PREFIX)]

    if as_json:
        _output_json(
            [
                {
                    "id": a.id,
                    "name": a.name,
                    "trust_tier": a.trust_tier,
                    "source_uri": a.source_uri,
                }
                for a in descriptors
            ]
        )
        return

    if not descriptors:
        click.echo("No MCP descriptors found.")
        return
    for descriptor in descriptors:
        display_name = descriptor.name[len(MCP_PREFIX) :]
        click.echo(
            f"{descriptor.id[:20]:<22} {descriptor.trust_tier:<12} {display_name}"
        )


@mcp_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def mcp_show(name: str, as_json: bool) -> None:
    """Show an MCP descriptor by name."""
    if not name.startswith(MCP_PREFIX):
        name = f"{MCP_PREFIX}{name}"

    store = _persistent_store()
    matches = store.get_assets_by_name(name)
    if not matches:
        raise click.ClickException(f"No MCP descriptor named '{name}'")

    descriptor = matches[0]
    content = json.loads(descriptor.content)
    if as_json:
        _output_json(
            {
                "id": descriptor.id,
                "name": descriptor.name,
                "content": content,
                "trust_tier": descriptor.trust_tier,
                "source_uri": descriptor.source_uri,
                "definition_hash": descriptor.definition_hash,
                "content_hash": descriptor.content_hash,
            }
        )
        return

    click.echo(f"id:              {descriptor.id}")
    click.echo(f"name:            {descriptor.name}")
    click.echo(f"trust_tier:      {descriptor.trust_tier}")
    click.echo(f"source_uri:      {descriptor.source_uri}")
    click.echo(f"definition_hash: {descriptor.definition_hash}")
    click.echo(f"content_hash:    {descriptor.content_hash}")
    click.echo("--- descriptor ---")
    click.echo(json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True))
