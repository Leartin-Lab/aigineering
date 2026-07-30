"""Read the signed definition/content graph."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store, _query_projection


@click.group("graph")
def graph_group() -> None:
    """Inspect content, signed definitions, and their assertions."""


@graph_group.command("contents")
@click.option("--json", "as_json", is_flag=True)
def graph_contents(as_json: bool) -> None:
    """List normalized content objects."""
    values = _query_projection(_persistent_store()).get_content_objects()
    if as_json:
        _output_json(values)
        return
    for value in values:
        click.echo(str(value["id"]))


@graph_group.command("definitions")
@click.option("--json", "as_json", is_flag=True)
def graph_definitions(as_json: bool) -> None:
    """List signed and migrated definitions."""
    values = _query_projection(_persistent_store()).get_asset_definitions()
    if as_json:
        _output_json(values)
        return
    for value in values:
        click.echo(f"{value['id']}  {value.get('name', '')}")


@graph_group.command("assertions")
@click.option("--definition-id", default="")
@click.option("--content-id", default="")
@click.option("--json", "as_json", is_flag=True)
def graph_assertions(definition_id: str, content_id: str, as_json: bool) -> None:
    """List definition-content assertions, optionally by endpoint."""
    values = _query_projection(_persistent_store()).get_definition_content_assertions(
        definition_id=definition_id, content_id=content_id
    )
    if as_json:
        _output_json(values)
        return
    for value in values:
        click.echo(
            f"{value['id']}  {value.get('relation_type', '')}  "
            f"{value['definition_id']} -> {value['content_id']}"
        )
