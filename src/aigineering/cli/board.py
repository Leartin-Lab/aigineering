"""CLI commands for project-local named workspace boards."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import click

from aigineering.cli._common import _output_json
from aigineering.local_boards import (
    BoardSetupPending,
    BoardRegistryError,
    archive_board,
    board_root_for_management,
    board_status,
    create_board,
    read_registry,
    resume_board_setup,
    select_board,
)


def _entry_json(board) -> dict:
    return {
        "name": board.name,
        "workspace": board.workspace,
        "domain_id": board.domain_id,
        "archived": board.archived,
        "availability": board_status(board),
    }


def _raise_board_error(exc: Exception, *, as_json: bool) -> None:
    if as_json:
        payload = {"ok": False, "error": "board_unavailable", "message": str(exc)}
        if isinstance(exc, BoardSetupPending):
            payload.update(
                {
                    "error": "board_setup_pending",
                    "workspace": exc.workspace,
                    "domain_id": exc.domain_id,
                    "registry_registered": exc.registry_registered,
                    "facts_retained": exc.facts_retained,
                    "phase": exc.phase,
                    "retry_command": exc.retry_command,
                }
            )
        _output_json(payload)
        raise click.exceptions.Exit(1) from exc
    raise click.ClickException(str(exc)) from exc


@click.group("board")
def board_group() -> None:
    """Manage named local workspace boards."""


@board_group.command("create")
@click.argument("name")
@click.option("--workspace", type=click.Path(path_type=Path, file_okay=False))
@click.option("--resume", "resume_setup", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def board_create(name: str, workspace: Path | None, resume_setup: bool, as_json: bool):
    """Create a managed board or register an initialized workspace."""
    root = board_root_for_management()
    try:
        board = (
            resume_board_setup(root, name, workspace=workspace)
            if resume_setup
            else create_board(root, name, workspace=workspace)
        )
    except (
        BoardRegistryError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ) as exc:
        _raise_board_error(exc, as_json=as_json)
    result = {"ok": True, "data": _entry_json(board)}
    if as_json:
        _output_json(result)
    else:
        click.echo(f"Board created: {board.name} ({board.domain_id})")
        click.echo(f"Workspace: {board.workspace}")


@board_group.command("list")
@click.option("--json", "as_json", is_flag=True)
def board_list(as_json: bool) -> None:
    """List registered boards and their pinned-domain availability."""
    root = board_root_for_management()
    try:
        registry = read_registry(root)
    except BoardRegistryError as exc:
        _raise_board_error(exc, as_json=as_json)
    rows = [_entry_json(board) for board in registry.boards]
    result = {"ok": True, "data": {"selected": registry.selected, "boards": rows}}
    if as_json:
        _output_json(result)
    elif not rows:
        click.echo("No boards registered.")
    else:
        for row in rows:
            selected = " *" if row["name"] == registry.selected else ""
            archived = " archived" if row["archived"] else ""
            click.echo(
                f"{row['name']}{selected}{archived}  {row['availability']}  "
                f"{row['domain_id']}  {row['workspace']}"
            )


@board_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def board_show(name: str, as_json: bool) -> None:
    """Show one registered board without claiming task readiness."""
    root = board_root_for_management()
    try:
        board = read_registry(root).by_name(name)
        if board is None:
            raise BoardRegistryError(f"board {name!r} does not exist")
    except BoardRegistryError as exc:
        _raise_board_error(exc, as_json=as_json)
    row = _entry_json(board)
    if as_json:
        _output_json({"ok": True, "data": row})
    else:
        click.echo(f"{board.name}: {row['availability']}")
        click.echo(f"Domain: {board.domain_id}")
        click.echo(f"Workspace: {board.workspace}")
        click.echo(f"Archived: {str(board.archived).lower()}")


@board_group.command("switch")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def board_switch(name: str, as_json: bool) -> None:
    """Select one available, non-archived board for ordinary commands."""
    root = board_root_for_management()
    try:
        board = select_board(root, name)
    except (BoardRegistryError, OSError) as exc:
        _raise_board_error(exc, as_json=as_json)
    if as_json:
        _output_json({"ok": True, "data": _entry_json(board)})
    else:
        click.echo(f"Selected board: {board.name} ({board.domain_id})")


@board_group.command("archive")
@click.argument("name")
@click.option("--restore", is_flag=True, help="Restore an archived board.")
@click.option("--json", "as_json", is_flag=True)
def board_archive(name: str, restore: bool, as_json: bool) -> None:
    """Archive a board or restore it without changing the selected name."""
    root = board_root_for_management()
    try:
        board = archive_board(root, name, restore=restore)
    except (BoardRegistryError, OSError) as exc:
        _raise_board_error(exc, as_json=as_json)
    if as_json:
        _output_json({"ok": True, "data": _entry_json(board)})
    else:
        verb = "Restored" if restore else "Archived"
        click.echo(f"{verb} board: {board.name}")
