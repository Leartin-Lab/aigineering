"""aig — Aigineering command-line interface (entry point)."""

from __future__ import annotations

import click
import os
from pathlib import Path

# ── Main CLI group ───────────────────────────────────────────────────────────


@click.group()
@click.option(
    "--offline", is_flag=True, help="Inspect selected local audit views without Redis."
)
@click.option(
    "-C",
    "--workspace",
    "workspace",
    type=click.Path(path_type=Path, exists=True, file_okay=False, dir_okay=True),
    help="Run from this existing workspace directory.",
)
@click.option("--board", "board_name", default=None, help="Run in one named board.")
@click.pass_context
def cli(
    ctx: click.Context,
    workspace: Path | None,
    board_name: str | None,
    offline: bool,
) -> None:
    """aig — Aigineering ACM runtime CLI."""
    ctx.ensure_object(dict)
    ctx.obj["offline"] = offline
    initial_cwd = Path.cwd()
    if workspace is not None and board_name is not None:
        raise click.UsageError("--board and --workspace/-C are mutually exclusive")
    if ctx.invoked_subcommand == "board":
        if board_name is not None:
            raise click.UsageError("board management commands cannot use --board")
        if workspace is not None:
            os.chdir(workspace.resolve())
            ctx.call_on_close(lambda: os.chdir(initial_cwd))
        return
    if workspace is not None:
        os.chdir(workspace.resolve())
        ctx.call_on_close(lambda: os.chdir(initial_cwd))
    else:
        from aigineering.local_boards import (
            BoardRegistryError,
            resolve_board_workspace,
        )

        try:
            selected_workspace = resolve_board_workspace(
                initial_cwd, explicit_name=board_name
            )
        except (BoardRegistryError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        if selected_workspace is not None:
            os.chdir(selected_workspace)
            ctx.call_on_close(lambda: os.chdir(initial_cwd))


# ── Register subcommand modules ──────────────────────────────────────────────

from aigineering.cli.run import run, demo  # noqa: E402
from aigineering.cli.trace import trace, audit  # noqa: E402
from aigineering.cli.recover import recover  # noqa: E402
from aigineering.cli.replay import replay  # noqa: E402
from aigineering.cli.session import session  # noqa: E402
from aigineering.cli.worker import worker  # noqa: E402
from aigineering.cli.retry import retry  # noqa: E402
from aigineering.cli.verify import verify, readiness  # noqa: E402
from aigineering.cli.asset import asset_group  # noqa: E402
from aigineering.cli.contract import contract_group  # noqa: E402
from aigineering.cli.task import task_group  # noqa: E402
from aigineering.cli.behavior import behavior_group  # noqa: E402
from aigineering.cli.repl import repl  # noqa: E402
from aigineering.cli.serve import serve  # noqa: E402
from aigineering.cli.skill import skill_group  # noqa: E402
from aigineering.cli.mcp import mcp_group  # noqa: E402
from aigineering.cli.capability import capability_group  # noqa: E402
from aigineering.cli.domain import domain_group  # noqa: E402
from aigineering.cli.board import board_group  # noqa: E402
from aigineering.cli.cache import cache_group  # noqa: E402
from aigineering.cli.redis import redis_group  # noqa: E402
from aigineering.cli.graph import graph_group  # noqa: E402
from aigineering.cli.fleet import fleet_group  # noqa: E402

from aigineering.cli.artifact import artifact_group  # noqa: E402
from aigineering.cli.method import method_group  # noqa: E402

cli.add_command(artifact_group)
cli.add_command(fleet_group)
cli.add_command(cache_group)
cli.add_command(redis_group)
cli.add_command(graph_group)
cli.add_command(serve)
cli.add_command(repl)
cli.add_command(capability_group)
cli.add_command(domain_group)
cli.add_command(board_group)
cli.add_command(mcp_group)
cli.add_command(skill_group)
cli.add_command(behavior_group)
cli.add_command(run)
cli.add_command(demo)
cli.add_command(trace)
cli.add_command(audit)
cli.add_command(replay)
cli.add_command(session)
cli.add_command(worker)
cli.add_command(recover)
cli.add_command(retry)
cli.add_command(verify)
cli.add_command(readiness)
cli.add_command(asset_group)
cli.add_command(task_group)
cli.add_command(contract_group)
cli.add_command(method_group)

from aigineering.cli.task_board import register_task_board_commands  # noqa: E402
from aigineering.cli.task_collaboration import register_task_collaboration_commands  # noqa: E402
from aigineering.cli.task_handoff import register_task_handoff_commands  # noqa: E402
from aigineering.cli.task_events import register_task_event_commands  # noqa: E402
from aigineering.cli.task_policy_review import register_task_policy_review_commands  # noqa: E402
from aigineering.cli.task_revision import register_task_revision_commands  # noqa: E402
from aigineering.cli.task_dependencies import register_task_dependency_commands  # noqa: E402
from aigineering.cli.task_assignment import register_task_assignment_commands  # noqa: E402

register_task_board_commands(task_group)
register_task_collaboration_commands(task_group)
register_task_handoff_commands(task_group)
register_task_event_commands(task_group)
register_task_policy_review_commands(task_group)
register_task_revision_commands(task_group)
register_task_dependency_commands(task_group)
register_task_assignment_commands(task_group)

from aigineering.cli.infrastructure import protect_commands  # noqa: E402

protect_commands(cli)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
