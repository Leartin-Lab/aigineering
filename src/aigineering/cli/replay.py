"""aig replay command."""

from __future__ import annotations

from typing import Optional

import click
from pathlib import Path

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
    "--sqlite-db",
    type=click.Path(path_type=Path),
    default=Path(".aig/store.db"),
    show_default=True,
    help="SQLite runtime store used to verify replayed facts.",
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
    sqlite_db: Path,
) -> None:
    """Replay a session from persisted data and validate consistency."""
    if replay_all_flag:
        results = replay_all(sqlite_path=str(sqlite_db))
        if not results:
            if json_output:
                _output_json([])
            else:
                click.echo("No sessions found.")
            return
        if json_output:
            _output_json([_build_replay_json_result(r) for r in results])
            if any(not r.get("consistent", False) for r in results):
                raise click.exceptions.Exit(1)
            return
        for r in results:
            _print_replay_result(r)
            click.echo("")
        if any(not r.get("consistent", False) for r in results):
            raise click.exceptions.Exit(1)
        return

    if not session_id:
        if json_output:
            _output_json(
                {"error": "Usage: aig replay <session_id>  or  aig replay --all"}
            )
        else:
            click.echo("Usage: aig replay <session_id>  or  aig replay --all")
        return

    result = replay_session(session_id, sqlite_path=str(sqlite_db))
    if "error" in result:
        if json_output:
            _output_replay_json(result)
        else:
            click.echo(result["error"])
        raise click.exceptions.Exit(1)

    if json_output:
        _output_replay_json(result)
        if not result.get("consistent", False):
            raise click.exceptions.Exit(1)
        return

    _print_replay_result(result)
    if not result.get("consistent", False):
        raise click.exceptions.Exit(1)


def _output_replay_json(result: dict) -> None:
    _output_json(_build_replay_json_result(result))


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
        "legacy_fragment_count_scope": result.get("legacy_fragment_count_scope", []),
        "candidate_decision_count": result.get("candidate_decision_count", 0),
        "accepted_candidate_count": result.get("accepted_candidate_count", 0),
        "rejected_candidate_count": result.get("rejected_candidate_count", 0),
        "effect_count": result.get("effect_count", 0),
        "accepted_output_count": result.get("accepted_output_count", 0),
        "manifest_trace_mismatch": result.get("manifest_trace_mismatch"),
        "consistent": result.get("consistent", False),
        "integrity": result.get("integrity"),
        "trace_mismatches": result.get("trace_mismatches"),
        "missing_asset_ids": result.get("missing_asset_ids"),
        "not_checked": result.get("not_checked", []),
        "seal_mismatches": result.get("seal_mismatches"),
        "error": result.get("error"),
        "error_type": result.get("error_type"),
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
    click.echo(
        "  Candidate decisions: "
        f"{result.get('candidate_decision_count', 0)} "
        f"({result.get('accepted_candidate_count', 0)} accepted, "
        f"{result.get('rejected_candidate_count', 0)} rejected)"
    )
    click.echo(
        f"  Candidate effects: {result.get('effect_count', 0)}; "
        f"accepted outputs/assets: {result.get('accepted_output_count', 0)}"
    )
    click.echo(
        "  Fragment counts cover legacy events: "
        f"{result.get('legacy_fragment_count_scope', [])}"
    )

    consistent = result.get("consistent", False)
    if consistent:
        click.echo("  Consistency: ✓ local replay and SQLite checks passed")
    else:
        duplicates = result.get("duplicate_ids", [])
        integrity = result.get("integrity")
        mismatches = result.get("trace_mismatches")
        missing_assets = result.get("missing_asset_ids")
        if integrity is not None and not integrity.get("consistent", False):
            click.echo("  Consistency: ✗ SQLite integrity check failed")
        elif mismatches:
            click.echo(f"  Consistency: ✗ SQLite trace mismatch: {mismatches}")
        elif missing_assets:
            click.echo(f"  Consistency: ✗ SQLite assets missing: {missing_assets}")
        elif duplicates:
            click.echo(f"  Consistency: ✗ duplicate asset IDs: {duplicates}")
        else:
            click.echo("  Consistency: ✗ not fully checked")

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
