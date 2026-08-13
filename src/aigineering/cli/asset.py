"""CLI commands for asset injection and inspection (control-plane ingress)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from aigineering.cli._common import _output_json, _persistent_store, _query_projection
from aigineering.cli._candidate import commit_local_effect, require_accepted
from aigineering.core.asset_versions import (
    create_replacement_claim,
    create_slice_asset,
    list_versions,
    resolve_latest,
)
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.verification import verify_replacement_claims
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    replacement_claim_effect,
)


@click.group("asset")
def asset_group() -> None:
    """Inject and inspect assets through the control plane."""
    pass


@asset_group.command("add")
@click.option("--name", required=True, help="Asset name.")
@click.option("--content", default=None, help="Inline text content.")
@click.option(
    "--content-file",
    type=click.Path(exists=True),
    default=None,
    help="Read content from a file.",
)
@click.option(
    "--content-json",
    type=click.Path(exists=True),
    default=None,
    help="Read JSON content and store as JSON string.",
)
@click.option("--origin", default="human", help="Provenance origin.")
@click.option("--trust-tier", default="human", help="Trust tier.")
@click.option("--source-uri", default="", help="Source reference URI.")
@click.option(
    "--promptable/--no-promptable",
    default=True,
    help="Whether the asset may be disclosed to workers.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def asset_add(
    name: str,
    content: str | None,
    content_file: str | None,
    content_json: str | None,
    origin: str,
    trust_tier: str,
    source_uri: str,
    promptable: bool,
    as_json: bool,
) -> None:
    """Inject an asset into the runtime store.

    Content may be supplied inline (--content), from a file
    (--content-file), or as JSON (--content-json).

    Protected runtime namespaces are rejected by default.
    """
    if content_file and content_json:
        raise click.UsageError("Cannot use both --content-file and --content-json")
    if content and (content_file or content_json):
        raise click.UsageError(
            "--content is mutually exclusive with --content-file/--content-json"
        )

    if content_file:
        content = Path(content_file).read_text()
    elif content_json:
        content = Path(content_json).read_text()
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            raise click.UsageError(f"--content-json file is not valid JSON: {e}")
    elif content is None:
        raise click.UsageError(
            "One of --content, --content-file, or --content-json is required"
        )

    store = _persistent_store()
    try:
        proposal = build_control_plane_asset(
            name=name,
            content=content,
            origin=origin,
            trust_tier=trust_tier,
            source_uri=source_uri,
            promptable=promptable,
            content_type="application/json" if content_json else "text",
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
        click.echo(f"Asset injected: {asset.name} ({asset.id[:16]}...)")
        click.echo(f"  trust_tier: {asset.trust_tier}")


@asset_group.command("ls")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def asset_list(as_json: bool) -> None:
    """List injected (control-plane) assets in the store."""
    store = _persistent_store()
    all_assets = _query_projection(store).get_all_assets()
    if as_json:
        result = [
            {
                "id": a.id,
                "name": a.name,
                "trust_tier": a.trust_tier,
                "origin": a.origin,
            }
            for a in all_assets
        ]
        _output_json(result)
    else:
        if not all_assets:
            click.echo("No assets found.")
        for a in all_assets:
            click.echo(f"{a.id[:20]:<22} {a.trust_tier:<12} {a.name}")


@asset_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def asset_show(name: str, as_json: bool) -> None:
    """Show asset content and metadata by name."""
    store = _persistent_store()
    matches = _query_projection(store).get_assets_by_name(name)
    if not matches:
        raise click.ClickException(f"No asset named '{name}'")

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
        click.echo("--- content ---")
        click.echo(asset.content)


@asset_group.command("slice")
@click.argument("name")
@click.option("--slice-name", required=True, help="Name for the sliced asset.")
@click.option(
    "--range", "range_spec", default="", help="Range specifier for the slice."
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def asset_slice(name: str, slice_name: str, range_spec: str, as_json: bool) -> None:
    """Create a new asset that is a slice of an existing asset.

    The slice is a separate asset with lineage linking back to the source.
    """
    store = _persistent_store()
    source = resolve_latest(store, name)
    if source is None:
        raise click.ClickException(f"No asset named '{name}' found in store.")

    try:
        sliced = create_slice_asset(
            source,
            slice_name=slice_name,
            range_spec=range_spec,
        )
    except ValueError as e:
        raise click.ClickException(str(e))
    try:
        decision = require_accepted(
            commit_local_effect(
                store,
                asset_proposal_effect(sliced),
                idempotency_key=f"asset-slice:{sliced.id}",
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    sliced = decision.assets[0]
    claim = create_replacement_claim(
        source_asset_id=source.id,
        replacement_asset_id=sliced.id,
        definition_hash=sliced.definition_hash,
        claim_type="slice",
        lineage_id=source.lineage_id or source.id,
        range_spec=range_spec,
    )
    verification = verify_replacement_claims(store, [claim])
    if verification["fail_count"]:
        issues = verification["results"][0]["issues"]
        raise click.ClickException("; ".join(issues))
    try:
        require_accepted(
            commit_local_effect(
                store,
                replacement_claim_effect(claim),
                idempotency_key=f"asset-slice-claim:{claim.id}",
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _output_json(
            {
                "id": sliced.id,
                "name": sliced.name,
                "lineage_id": sliced.lineage_id,
                "definition_hash": sliced.definition_hash,
                "claim_id": claim.id,
                "range_spec": claim.range_spec,
            }
        )
    else:
        click.echo(f"Slice created: {sliced.name} ({sliced.id[:16]}...)")
        click.echo(f"  lineage:     {sliced.lineage_id}")
        click.echo(f"  definition:  {sliced.definition_hash}")


@asset_group.command("replace")
@click.argument("source_id")
@click.argument("replacement_id")
@click.option(
    "--claim-type",
    default="replacement",
    help="Claim type (replacement, slice, summary, redaction, equivalent_input).",
)
@click.option(
    "--range",
    "range_spec",
    default="",
    help="Required deterministic source range for a slice claim.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def asset_replace(
    source_id: str,
    replacement_id: str,
    claim_type: str,
    range_spec: str,
    as_json: bool,
) -> None:
    """Create a replacement claim linking source to replacement asset."""
    store = _persistent_store()

    source = store.get_asset(source_id)
    if source is None:
        raise click.ClickException(f"Source asset '{source_id}' not found.")
    replacement = store.get_asset(replacement_id)
    if replacement is None:
        raise click.ClickException(f"Replacement asset '{replacement_id}' not found.")

    if claim_type != "slice" and range_spec:
        raise click.ClickException("--range is only valid with --claim-type slice")
    claim = create_replacement_claim(
        source_asset_id=source_id,
        replacement_asset_id=replacement_id,
        definition_hash=(
            replacement.definition_hash
            if claim_type == "slice"
            else source.definition_hash
        ),
        claim_type=claim_type,
        signed_by="",
        provenance_seal="",
        lineage_id=(source.lineage_id or source.id) if claim_type == "slice" else "",
        range_spec=range_spec,
    )
    verification = verify_replacement_claims(store, [claim])
    if verification["fail_count"]:
        issues = verification["results"][0]["issues"]
        raise click.ClickException("; ".join(issues))
    try:
        require_accepted(
            commit_local_effect(
                store,
                replacement_claim_effect(claim),
                idempotency_key=f"asset-relation:{claim.id}",
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    claim = next(
        item for item in store.get_claims_for_asset(source_id) if item.id == claim.id
    )

    if as_json:
        _output_json(
            {
                "claim_id": claim.id,
                "source_asset_id": claim.source_asset_id,
                "replacement_asset_id": claim.replacement_asset_id,
                "claim_type": claim.claim_type,
                "derivation_version": claim.derivation_version,
                "range_spec": claim.range_spec,
            }
        )
    else:
        click.echo(f"Replacement claim created: {claim.id}")
        click.echo(f"  source:      {source_id[:20]}...")
        click.echo(f"  replacement: {replacement_id[:20]}...")
        click.echo(f"  claim_type:  {claim.claim_type}")


@asset_group.command("versions")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def asset_versions(name: str, as_json: bool) -> None:
    """List all versions of an asset by name."""
    store = _persistent_store()
    versions = list_versions(store, name)
    if not versions:
        click.echo(f"No versions found for '{name}'.")
        return

    if as_json:
        _output_json(
            [
                {
                    "id": v.id,
                    "content_hash": v.content_hash,
                    "definition_hash": v.definition_hash,
                    "lineage_id": v.lineage_id,
                }
                for v in versions
            ]
        )
    else:
        click.echo(f"Versions of '{name}':")
        for v in versions:
            marker = "→" if v.id == versions[-1].id else " "
            click.echo(
                f"  {marker} {v.id[:24]:<26} content_hash={v.content_hash[:16]}..."
            )


@asset_group.command("lineage")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def asset_lineage(name: str, as_json: bool) -> None:
    """Show the lineage chain for an asset by name."""
    store = _persistent_store()
    matches = store.get_assets_by_name(name)
    if not matches:
        raise click.ClickException(f"No asset named '{name}' found.")

    asset = matches[0]
    lineage_chain: list[dict] = []
    current = asset
    visited: set[str] = set()

    while current and current.lineage_id and current.lineage_id not in visited:
        visited.add(current.id)
        lineage_chain.append(
            {
                "id": current.id,
                "name": current.name,
                "lineage_id": current.lineage_id,
                "definition_hash": current.definition_hash,
            }
        )
        # Find the source asset with matching lineage_id
        found = False
        for a in store.get_all_assets():
            if a.id == current.lineage_id:
                current = a
                found = True
                break
        if not found:
            break

    # Add final node
    if current.id not in visited:
        lineage_chain.append(
            {
                "id": current.id,
                "name": current.name,
                "lineage_id": current.lineage_id,
                "definition_hash": current.definition_hash,
            }
        )

    if as_json:
        _output_json(lineage_chain)
    else:
        click.echo(f"Lineage for '{name}':")
        for i, node in enumerate(lineage_chain):
            prefix = "├─ " if i < len(lineage_chain) - 1 else "└─ "
            click.echo(f"  {prefix}{node['id'][:24]:<26} {node['name']}")
