"""CLI commands for loading skill descriptor/content assets."""

from __future__ import annotations

import json

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.skill_loader import SkillLoader
from aigineering.core.trace import create_entry


@click.group("skill")
def skill_group() -> None:
    """Load and inspect skill assets."""


@skill_group.command("load")
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def skill_load(directory: str, as_json: bool) -> None:
    """Load skills from DIRECTORY into the local store."""
    store = _persistent_store()
    ingress = RuntimeIngress(store, store)
    loader = SkillLoader()
    try:
        manifests = loader.scan([directory])
        assets = loader.load(store, ingress=ingress)
    except ValueError as e:
        raise click.ClickException(str(e))
    if hasattr(store, "append"):
        for asset in assets:
            store.append(
                create_entry(
                    contract_id="control_plane",
                    event_type="asset_injected",
                    parent_id=asset.id,
                    relation_type="skill_asset",
                    relation_target=asset.name,
                    accepted_fragments=[
                        json.dumps(
                            {
                                "asset_id": asset.id,
                                "origin": asset.origin,
                                "trust_tier": asset.trust_tier,
                            },
                            sort_keys=True,
                        )
                    ],
                )
            )

    result = {
        "loaded_manifests": [m.name for m in manifests],
        "asset_ids": [a.id for a in assets],
        "asset_names": [a.name for a in assets],
    }
    if as_json:
        _output_json(result)
    else:
        click.echo(f"Loaded {len(manifests)} skill(s).")
        for name in result["asset_names"]:
            click.echo(f"  {name}")


@skill_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def skill_list(as_json: bool) -> None:
    """List skill capability descriptors in the local store."""
    store = _persistent_store()
    skills = [
        a for a in store.get_all_assets() if a.name.startswith("_skill_capability_")
    ]
    result = []
    for asset in skills:
        try:
            content = json.loads(asset.content)
        except json.JSONDecodeError:
            content = {}
        result.append(
            {
                "id": asset.id,
                "name": asset.name,
                "skill": content.get("name", asset.name),
                "trust_tier": asset.trust_tier,
                "content_hash": content.get("content_hash", ""),
            }
        )

    if as_json:
        _output_json(result)
    else:
        if not result:
            click.echo("No skills found.")
            return
        for row in result:
            click.echo(f"{row['id'][:20]:<22} {row['trust_tier']:<12} {row['skill']}")
