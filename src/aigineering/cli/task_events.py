"""Read-only task event and watch commands over the required Redis feed."""

from __future__ import annotations

import json
import math
import time

import click

from aigineering.application import collaboration_service
from aigineering.cli._common import _output_json, _persistent_snapshot
from aigineering.task_views import contract_tree_ids


def _read_events(contract_id: str | None, after: int, limit: int):
    store = _persistent_snapshot()
    try:
        revision = store.get_runtime_revision()
        ids = None
        if contract_id is not None:
            ids = contract_tree_ids(store.get_all_contracts(), contract_id)
            if not ids:
                raise LookupError(
                    "Task not found; use an exact Contract ID from 'aig task list'."
                )
        service = collaboration_service(store)
        service.sync()
        events = [
            event
            for event in service.events(after_revision=after, count=limit)
            if event["revision"] <= revision
        ]
        cursor = events[-1]["revision"] if events else after
        selected = (
            events
            if ids is None
            else [event for event in events if ids.intersection(event["contract_ids"])]
        )
        return selected, cursor, revision
    finally:
        store.close()


def _emit_event_error(message: str, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": "task_not_found", "message": message})
        raise click.exceptions.Exit(1)
    raise click.ClickException(message)


@click.command("events")
@click.argument("contract_id", required=False)
@click.option("--after", type=click.IntRange(min=0), default=0, show_default=True)
@click.option("--limit", type=click.IntRange(1, 10000), default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def task_events(contract_id: str | None, after: int, limit: int, as_json: bool) -> None:
    """Read committed-record events, optionally restricted to one immutable task tree."""
    try:
        events, cursor, revision = _read_events(contract_id, after, limit)
    except LookupError as exc:
        _emit_event_error(str(exc), as_json)
        return
    if as_json:
        _output_json(
            {"ok": True, "data": events, "next_revision": cursor, "revision": revision}
        )
    else:
        for event in events:
            click.echo(
                f"{event['revision']} {event['record_type']} {event['record_id']}"
            )
        click.echo(f"next revision: {cursor}")


@click.command("watch")
@click.argument("contract_id", required=False)
@click.option("--after", type=click.IntRange(min=0), default=0, show_default=True)
@click.option("--timeout", type=float, default=30.0, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per line.")
def task_watch(
    contract_id: str | None, after: int, timeout: float, as_json: bool
) -> None:
    """Observe a task tree or workspace feed for a bounded period; never dispatch work."""
    if not math.isfinite(timeout) or timeout < 0:
        raise click.BadParameter(
            "must be finite and non-negative", param_hint="--timeout"
        )
    deadline = time.monotonic() + timeout
    cursor = after
    while True:
        try:
            events, next_cursor, revision = _read_events(contract_id, cursor, 100)
        except LookupError as exc:
            _emit_event_error(str(exc), as_json)
            return
        for event in events:
            if as_json:
                click.echo(
                    json.dumps(
                        {"ok": True, "kind": "event", "data": event}, ensure_ascii=False
                    )
                )
            else:
                click.echo(
                    f"{event['revision']} {event['record_type']} {event['record_id']}"
                )
        cursor = next_cursor
        if cursor < revision:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if as_json:
                click.echo(
                    json.dumps(
                        {"ok": True, "kind": "watch_finished", "next_revision": cursor}
                    )
                )
            return
        store = _persistent_snapshot()
        try:
            collaboration_service(store).wait(cursor, timeout=min(0.25, remaining))
        finally:
            store.close()


def register_task_event_commands(group: click.Group) -> None:
    group.add_command(task_events)
    group.add_command(task_watch)
