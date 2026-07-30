"""Inspect and rebuild the optional disposable query projection."""

from __future__ import annotations

import click

from aigineering.cli._common import (
    _output_json,
    _persistent_store,
    _query_projection,
)
from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.core.query_projection import StoreQueryProjection


@click.group("cache")
def cache_group() -> None:
    """Inspect the optional Redis query projection."""


@cache_group.command("status")
@click.option("--json", "as_json", is_flag=True)
def cache_status(as_json: bool) -> None:
    """Show Redis availability, generation, and authoritative watermark."""
    store = _persistent_store()
    status = _query_projection(store).status()
    if as_json:
        _output_json(status)
        return
    for name, value in status.items():
        click.echo(f"{name}: {value}")


@cache_group.command("rebuild")
@click.option("--json", "as_json", is_flag=True)
def cache_rebuild(as_json: bool) -> None:
    """Rebuild and atomically activate a Redis generation from SQLite."""
    store = _persistent_store()
    projection = _query_projection(store)
    if isinstance(projection, StoreQueryProjection):
        status = projection.status()
        reason = status.get("reason") or "AIGINEERING_REDIS_URL is not configured"
        raise click.ClickException(f"Redis query projection unavailable: {reason}")
    try:
        snapshot = projection.rebuild()
    except QueryProjectionUnavailable as exc:
        raise click.ClickException(
            f"Redis query projection unavailable: {exc}"
        ) from exc
    result = projection.status()
    result["rebuilt_digest"] = snapshot.digest
    if as_json:
        _output_json(result)
        return
    click.echo(f"Redis query projection rebuilt: {snapshot.digest}")
