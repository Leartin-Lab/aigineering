"""CLI commands for non-MCP capability descriptor assets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from aigineering.cli._common import _output_json, _persistent_store, _query_projection
from aigineering.cli._candidate import commit_local_effect, require_accepted
from aigineering.core.capability_descriptors import (
    create_memory_descriptor,
    create_persona_descriptor,
    create_tool_descriptor,
    verify_descriptor,
)
from aigineering.core.tool_schema import (
    DEFAULT_MAX_OUTPUT_BYTES,
    validate_schema_document,
)
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.types import ToolSpec

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


def _store_descriptor(descriptor, kind: str):
    if not verify_descriptor(descriptor, kind=kind):
        raise click.ClickException(
            "Capability descriptor failed trust gate; use trust_tier >= configured."
        )
    store = _persistent_store()
    try:
        decision = require_accepted(
            commit_local_effect(
                store,
                asset_proposal_effect(descriptor),
                idempotency_key=f"capability:{kind}:{descriptor.id}",
            )
        )
        if len(decision.assets) != 1:
            raise ValueError("accepted capability publication returned no descriptor")
        return decision.assets[0]
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


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
    "--output-schema-json",
    type=click.Path(exists=True),
    default=None,
    help="Optional JSON Schema file for tool output.",
)
@click.option(
    "--version",
    default="0.1.0",
    show_default=True,
    help="Tool contract version.",
)
@click.option(
    "--max-output-bytes",
    type=click.IntRange(min=1),
    default=DEFAULT_MAX_OUTPUT_BYTES,
    show_default=True,
    help="Maximum UTF-8 result size.",
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
    output_schema_json: str | None,
    version: str,
    max_output_bytes: int,
    trust_tier: str,
    source_uri: str,
    as_json: bool,
) -> None:
    """Create a tool capability descriptor."""
    input_schema = _read_json_object(input_schema_json)
    output_schema = _read_json_object(output_schema_json) if output_schema_json else {}
    try:
        validate_schema_document(input_schema, path="$.input_schema")
        validate_schema_document(output_schema, path="$.output_schema")
        ToolSpec(
            name=name,
            input_schema=input_schema,
            output_schema=output_schema,
            version=version,
            max_output_bytes=max_output_bytes,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    descriptor = create_tool_descriptor(
        name=name,
        description=description,
        input_schema=input_schema,
        trust_tier=trust_tier,
        source_uri=source_uri,
        output_schema=output_schema,
        version=version,
        max_output_bytes=max_output_bytes,
    )
    descriptor = _store_descriptor(descriptor, "tool")
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
    descriptor = _store_descriptor(descriptor, "memory")
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
    descriptor = _store_descriptor(descriptor, "persona")
    _emit_descriptor(descriptor, as_json)


@capability_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def list_capabilities(as_json: bool) -> None:
    """List tool, memory, and persona capability descriptors."""
    store = _persistent_store()
    descriptors = sorted(
        (
            asset
            for asset in _query_projection(store).get_all_assets()
            if any(asset.name.startswith(prefix) for prefix in _PREFIXES)
        ),
        key=lambda asset: (asset.name, asset.id),
    )

    if as_json:
        _output_json([_descriptor_json(asset) for asset in descriptors])
        return
    if not descriptors:
        click.echo("No capability descriptors found.")
        return
    for asset in descriptors:
        click.echo(f"{asset.id[:20]:<22} {asset.trust_tier:<12} {asset.name}")


@capability_group.command("show")
@click.argument("name", required=False)
@click.option(
    "--id", "descriptor_id", default=None, help="Select one exact descriptor ID."
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def show_capability(name: str | None, descriptor_id: str | None, as_json: bool) -> None:
    """Show a capability descriptor by logical name, storage name, or exact ID."""
    if (name is None) == (descriptor_id is None):
        raise click.UsageError("provide exactly one of NAME or --id")
    if descriptor_id is not None and not descriptor_id.strip():
        raise click.UsageError("--id must be a non-empty descriptor ID")

    store = _persistent_store()
    projection = _query_projection(store)
    descriptors = [
        asset
        for asset in projection.get_all_assets()
        if any(asset.name.startswith(prefix) for prefix in _PREFIXES)
    ]
    if descriptor_id is not None:
        matches = [asset for asset in descriptors if asset.id == descriptor_id]
        if not matches:
            raise click.ClickException(
                f"No capability descriptor with ID '{descriptor_id}'"
            )
    else:
        assert name is not None
        if any(name.startswith(prefix) for prefix in _PREFIXES):
            matches = [asset for asset in descriptors if asset.name == name]
        else:
            matches = [
                asset
                for asset in descriptors
                if _descriptor_logical_name(asset.name) == name
            ]
        if not matches:
            raise click.ClickException(f"No capability descriptor named '{name}'")
    if len(matches) > 1:
        choices = "\n".join(
            f"  {asset.name} version={_descriptor_version(asset)} id={asset.id}"
            for asset in sorted(matches, key=lambda item: (item.name, item.id))
        )
        raise click.ClickException(
            f"Capability selector is ambiguous; select one descriptor with --id:\n{choices}"
        )
    _emit_descriptor(matches[0], as_json)


def _descriptor_logical_name(asset_name: str) -> str:
    for prefix in _PREFIXES:
        if asset_name.startswith(prefix):
            return asset_name[len(prefix) :]
    return asset_name


def _descriptor_version(asset) -> str:
    try:
        content = json.loads(asset.content)
    except (TypeError, ValueError):
        return "unknown"
    version = content.get("version") if isinstance(content, dict) else None
    return version if isinstance(version, str) else "unknown"


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
    click.echo(
        json.dumps(data["content"], ensure_ascii=False, indent=2, sort_keys=True)
    )
