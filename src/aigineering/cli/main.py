"""aig — Aigineering command-line interface (entry point)."""

from __future__ import annotations

import click

# ── Main CLI group ───────────────────────────────────────────────────────────


@click.group()
def cli() -> None:
    """aig — Aigineering ACM runtime CLI."""


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

cli.add_command(serve)
cli.add_command(repl)
cli.add_command(capability_group)
cli.add_command(domain_group)
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
