"""CLI commands for non-MCP capability descriptor assets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.core.capability_descriptors import (
    create_memory_descriptor,
    create_persona_descriptor,
    create_tool_descriptor,
    verify_descriptor,
)
from aigineering.core.trace import create_entry

_PREFIXES = (
    "_tool_capability_",
    "_memory_capability_",
    "_persona_capability_",
)


@click.group("capability")
def capability_group() -> None:
    """Manage tool, memory, and persona capability descriptors."""
    pass


def _read_json_object(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise click.UsageError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise click.UsageError(f"{path} must contain a JSON object")
    return data


def _store_descriptor(descriptor, kind: str) -> None:
    if not verify_descriptor(descriptor, kind=kind):
        raise click.ClickException(
            "Capability descriptor failed trust gate; use trust_tier >= configured."
        )
    store = _persistent_store()
    store.add_asset(descriptor)
    if hasattr(store, "append"):
        store.append(
            create_entry(
                contract_id="control_plane",
                event_type="asset_injected",
                parent_id=descriptor.id,
                relation_type=f"{kind}_capability",
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


def _descriptor_json(asset) -> dict[str, object]:
    return {
        "id": asset.id,
        "name": asset.name,
        "trust_tier": asset.trust_tier,
        "source_uri": asset.source_uri,
        "content": json.loads(asset.content),
        "definition_hash": asset.definition_hash,
        "content_hash": asset.content_hash,
    }


@capability_group.command("add-tool")
@click.option("--name", required=True, help="Tool capability name.")
@click.option("--description", default="", help="Tool description.")
@click.option(
    "--input-schema-json",
    required=True,
    type=click.Path(exists=True),
    help="JSON Schema file for tool input.",
)
@click.option(
    "--trust-tier",
    default="configured",
    show_default=True,
    help="Trust tier for the descriptor.",
)
@click.option("--source-uri", default="", help="Public source URI.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def add_tool(
    name: str,
    description: str,
    input_schema_json: str,
    trust_tier: str,
    source_uri: str,
    as_json: bool,
) -> None:
    """Create a tool capability descriptor."""
    descriptor = create_tool_descriptor(
        name=name,
        description=description,
        input_schema=_read_json_object(input_schema_json),
        trust_tier=trust_tier,
        source_uri=source_uri,
    )
    _store_descriptor(descriptor, "tool")
    _emit_descriptor(descriptor, as_json)


@capability_group.command("add-memory")
@click.option("--name", required=True, help="Memory capability name.")
@click.option("--source-uri", required=True, help="Public memory source URI.")
@click.option(
    "--trust-tier",
    default="configured",
    show_default=True,
    help="Trust tier for the descriptor.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def add_memory(name: str, source_uri: str, trust_tier: str, as_json: bool) -> None:
    """Create a memory capability descriptor."""
    descriptor = create_memory_descriptor(
        name=name,
        source_uri=source_uri,
        trust_tier=trust_tier,
    )
    _store_descriptor(descriptor, "memory")
    _emit_descriptor(descriptor, as_json)


@capability_group.command("add-persona")
@click.option("--name", required=True, help="Persona capability name.")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True),
    help="Persona text file.",
)
@click.option(
    "--trust-tier",
    default="configured",
    show_default=True,
    help="Trust tier for the descriptor.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def add_persona(
    name: str,
    file_path: str,
    trust_tier: str,
    as_json: bool,
) -> None:
    """Create a persona capability descriptor."""
    descriptor = create_persona_descriptor(
        name=name,
        content=Path(file_path).read_text(),
        trust_tier=trust_tier,
    )
    _store_descriptor(descriptor, "persona")
    _emit_descriptor(descriptor, as_json)


@capability_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def list_capabilities(as_json: bool) -> None:
    """List tool, memory, and persona capability descriptors."""
    store = _persistent_store()
    descriptors = [
        asset
        for asset in store.get_all_assets()
        if any(asset.name.startswith(prefix) for prefix in _PREFIXES)
    ]

    if as_json:
        _output_json([_descriptor_json(asset) for asset in descriptors])
        return
    if not descriptors:
        click.echo("No capability descriptors found.")
        return
    for asset in descriptors:
        click.echo(f"{asset.id[:20]:<22} {asset.trust_tier:<12} {asset.name}")


@capability_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def show_capability(name: str, as_json: bool) -> None:
    """Show a capability descriptor by exact asset name."""
    store = _persistent_store()
    matches = store.get_assets_by_name(name)
    if not matches or not any(name.startswith(prefix) for prefix in _PREFIXES):
        raise click.ClickException(f"No capability descriptor named '{name}'")
    _emit_descriptor(matches[0], as_json)


def _emit_descriptor(asset, as_json: bool) -> None:
    data = _descriptor_json(asset)
    if as_json:
        _output_json(data)
        return

    click.echo(f"id:              {asset.id}")
    click.echo(f"name:            {asset.name}")
    click.echo(f"trust_tier:      {asset.trust_tier}")
    click.echo(f"source_uri:      {asset.source_uri}")
    click.echo(f"definition_hash: {asset.definition_hash}")
    click.echo(f"content_hash:    {asset.content_hash}")
    click.echo("--- descriptor ---")
    click.echo(json.dumps(data["content"], ensure_ascii=False, indent=2, sort_keys=True))
