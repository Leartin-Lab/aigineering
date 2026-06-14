"""aig replay command."""

from __future__ import annotations

from typing import Optional

import click

from aigineering.cli._common import _output_json, _redact_sealed
from aigineering.core.replay import replay_all, replay_session
from aigineering.protocol.wire import session_to_dict, trace_entry_to_dict


@click.command("replay")
@click.argument("session_id", required=False)
@click.option(
    "--all",
    "replay_all_flag",
    is_flag=True,
    default=False,
    help="Replay all stored sessions",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def replay(
    session_id: Optional[str],
    replay_all_flag: bool,
    json_output: bool,
) -> None:
    """Replay a session from persisted data and validate consistency."""
    if replay_all_flag:
        results = replay_all()
        if not results:
            if json_output:
                _output_json([])
            else:
                click.echo("No sessions found.")
            return
        if json_output:
            _output_json([_build_replay_json_result(r) for r in results])
            return
        for r in results:
            _print_replay_result(r)
            click.echo("")
        return

    if not session_id:
        if json_output:
            _output_json(
                {"error": "Usage: aig replay <session_id>  or  aig replay --all"}
            )
        else:
            click.echo("Usage: aig replay <session_id>  or  aig replay --all")
        return

    result = replay_session(session_id)
    if "error" in result:
        if json_output:
            _output_json({"error": result["error"]})
        else:
            click.echo(result["error"])
        return

    if json_output:
        _output_replay_json(result)
        return

    _print_replay_result(result)


def _output_replay_json(result: dict) -> None:
    session = result.get("session")
    entries = result.get("entries", [])
    payload: dict = {
        "session": (
            _redact_sealed(session_to_dict(session)) if session is not None else None
        ),
        "entries": [trace_entry_to_dict(e) for e in entries],
        "accepted_count": result.get("accepted_count", 0),
        "rejected_count": result.get("rejected_count", 0),
        "consistent": result.get("consistent", False),
    }
    by_event = result.get("by_event", {})
    if by_event:
        payload["by_event"] = {k: len(v) for k, v in by_event.items()}
    duplicates = result.get("duplicate_ids")
    if duplicates:
        payload["duplicate_ids"] = duplicates
    _output_json(payload)


def _build_replay_json_result(result: dict) -> dict:
    session = result.get("session")
    entries = result.get("entries", [])
    payload: dict = {
        "session": (
            _redact_sealed(session_to_dict(session)) if session is not None else None
        ),
        "entries": [trace_entry_to_dict(e) for e in entries],
        "accepted_count": result.get("accepted_count", 0),
        "rejected_count": result.get("rejected_count", 0),
        "consistent": result.get("consistent", False),
    }
    by_event = result.get("by_event", {})
    if by_event:
        payload["by_event"] = {k: len(v) for k, v in by_event.items()}
    duplicates = result.get("duplicate_ids")
    if duplicates:
        payload["duplicate_ids"] = duplicates
    return payload


def _print_replay_result(result: dict) -> None:
    session = result.get("session")
    if session is None:
        return

    click.echo(f"Session: {session.id}")
    click.echo(f"  Root contract: {session.root_contract_id}")
    click.echo(f"  Created: {session.created_at}")

    entries = result.get("entries", [])
    by_event = result.get("by_event", {})

    click.echo(f"  Trace entries: {len(entries)}")
    for event_type in sorted(by_event):
        click.echo(f"    {event_type}: {len(by_event[event_type])}")

    accepted = result.get("accepted_count", 0)
    rejected = result.get("rejected_count", 0)
    click.echo(f"  Accepted fragments: {accepted}")
    click.echo(f"  Rejected fragments: {rejected}")

    consistent = result.get("consistent", False)
    if consistent:
        click.echo("  Consistency: ✓ no duplicate asset IDs")
    else:
        duplicates = result.get("duplicate_ids", [])
        click.echo(f"  Consistency: ✗ duplicate asset IDs: {duplicates}")

    click.echo("")
    click.echo("  Timeline:")
    for entry in entries:
        marker = "✓" if entry.event_type in ("activation", "complete") else "→"
        if entry.event_type == "projection":
            if entry.rejected_fragments and not entry.accepted_fragments:
                marker = "✗"
        click.echo(f"    {marker} [{entry.event_type}]", nl=False)
        if entry.event_type == "activation":
            click.echo(f" contract activated (budget: {entry.budget_remaining})")
        elif entry.event_type == "disclosure":
            worker = entry.worker_id or "worker"
            assets = entry.disclosed_assets or []
            click.echo(f" {assets} → {worker}")
        elif entry.event_type == "projection":
            accepted = len(entry.accepted_fragments)
            rejected_count = len(entry.rejected_fragments)
            click.echo(f" +{accepted} accepted, -{rejected_count} rejected")
        elif entry.event_type == "complete":
            click.echo(" outputs satisfied")
