"""Inspect and repair the required Redis collaboration component."""

from __future__ import annotations

from pathlib import Path

import click

from aigineering.application import (
    collaboration_service,
    query_projection,
    require_redis,
)
from aigineering.cli._common import _output_json, _persistent_snapshot
from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.redis_collaboration import RedisCollaborationUnavailable


@click.group("redis")
def redis_group() -> None:
    """Health, synchronization and rebuild of the required collaboration service."""


@redis_group.command("status")
@click.option("--json", "as_json", is_flag=True)
def redis_status(as_json: bool) -> None:
    """Check Redis and show the current domain's immutable-record relay lag."""
    try:
        require_redis()
        if not Path(".aig/store.db").is_file():
            payload = {
                "ok": True,
                "backend": "redis",
                "available": True,
                "required": True,
                "current": False,
                "reason": "domain_uninitialized",
            }
        else:
            store = _persistent_snapshot()
            try:
                payload = collaboration_service(store).status()
                payload["required"] = True
                payload["ok"] = bool(payload.get("available")) and bool(
                    payload.get("current")
                )
            finally:
                store.close()
    except (LookupError, ValueError):
        payload = {
            "ok": False,
            "backend": "redis",
            "available": True,
            "required": True,
            "current": False,
            "reason": "domain_uninitialized",
        }
    except QueryProjectionUnavailable:
        payload = {
            "ok": False,
            "backend": "redis",
            "available": False,
            "required": True,
            "current": False,
            "reason": "redis_unavailable",
        }
    if as_json:
        _output_json(payload)
    else:
        click.echo(
            f"redis: available={payload['available']} current={payload['current']} "
            f"reason={payload.get('reason', '')}"
        )
        if "lag" in payload:
            click.echo(f"relay lag: {payload['lag']}")
    if not payload["ok"]:
        raise click.exceptions.Exit(1)


def _sync(as_json: bool, *, rebuild: bool) -> None:
    require_redis()
    db_path = Path(".aig/store.db").resolve()
    try:
        store = _persistent_snapshot(db_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(
            "Existing initialized workspace Store required."
        ) from exc
    last_probe = None

    def fresh_routed_snapshot():
        nonlocal last_probe
        if last_probe is not None:
            last_probe.close()
            last_probe = None
        last_probe = _persistent_snapshot(db_path)
        return last_probe

    try:
        service = collaboration_service(store)
        if rebuild:
            service.rebuild(fresh_store_factory=fresh_routed_snapshot)
            final_store = last_probe or store
            snapshot = query_projection(final_store).rebuild()
            service = collaboration_service(final_store)
        else:
            service.sync()
            # Bring the fact-derived read indexes to the same source snapshot.
            query_projection(store).get_all_contracts()
            snapshot = None
        payload = service.status()
        payload["ok"] = bool(payload.get("current"))
        payload["required"] = True
        if snapshot is not None:
            payload["rebuilt_digest"] = snapshot.digest
        if as_json:
            _output_json(payload)
        else:
            click.echo(f"redis {'rebuilt' if rebuild else 'synchronized'}")
        if not payload["ok"]:
            raise click.exceptions.Exit(1)
    except RedisCollaborationUnavailable as exc:
        unavailable = exc.reason == "redis_unavailable"
        payload = {
            "ok": False,
            "backend": "redis",
            "available": not unavailable,
            "required": True,
            "current": False,
            "reason": exc.reason,
        }
        if as_json:
            _output_json(payload)
        else:
            click.echo(
                f"redis: available={payload['available']} current=False "
                f"reason={payload['reason']}"
            )
        raise click.exceptions.Exit(1)
    except LookupError as exc:
        raise click.ClickException(
            "Initialize this workspace with 'aig domain init'."
        ) from exc
    finally:
        if last_probe is not None:
            last_probe.close()
        store.close()


@redis_group.command("sync")
@click.option("--json", "as_json", is_flag=True)
def redis_sync(as_json: bool) -> None:
    """Replay pending committed records and refresh read indexes."""
    _sync(as_json, rebuild=False)


@redis_group.command("rebuild")
@click.option("--json", "as_json", is_flag=True)
def redis_rebuild(as_json: bool) -> None:
    """Rebuild only this domain's event feed and indexes from SQLite facts."""
    _sync(as_json, rebuild=True)
