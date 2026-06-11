"""aig session command group."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _redact_sealed
from aigineering.core.session import SessionStore
from aigineering.protocol.wire import session_to_dict


@click.group("session")
def session() -> None:
    """Manage session manifests."""


@session.command("ls")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def session_ls(json_output: bool) -> None:
    """List sessions with id and created_at."""
    store = SessionStore()
    sessions = store.list_sessions()
    if json_output:
        payload = [
            _redact_sealed(session_to_dict(s)) for s in sessions
        ]
        _output_json(payload)
        return
    if not sessions:
        click.echo("No sessions found.")
        return
    for s in sessions:
        click.echo(f"{s.id}  {s.created_at}")


@session.command("show")
@click.argument("session_id")
def session_show(session_id: str) -> None:
    """Show full session manifest."""
    store = SessionStore()
    s = store.get_session(session_id)
    if s is None:
        click.echo(f"Session '{session_id}' not found.")
        return
    click.echo(f"id:                {s.id}")
    click.echo(f"root_contract_id:  {s.root_contract_id}")
    click.echo(f"contract_ids:      {s.contract_ids}")
    click.echo(f"asset_ids:         {s.asset_ids}")
    click.echo(f"trace_ids:         {s.trace_ids}")
    click.echo(f"config_snapshot:   {s.config_snapshot}")
    click.echo(f"worker_snapshot:   {s.worker_snapshot}")
    click.echo(f"created_at:        {s.created_at}")
