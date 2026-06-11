"""aig — Aigineering command-line interface (entry point)."""

from __future__ import annotations

import click

# ── Main CLI group ───────────────────────────────────────────────────────────

@click.group()
def cli() -> None:
    """aig — Aigineering ACM runtime CLI."""


# ── Register subcommand modules ──────────────────────────────────────────────

from aigineering.cli.run import run, demo                     # noqa: E402
from aigineering.cli.trace import trace, audit                # noqa: E402
from aigineering.cli.replay import replay                     # noqa: E402
from aigineering.cli.session import session                   # noqa: E402
from aigineering.cli.worker import worker                     # noqa: E402
from aigineering.cli.retry import retry                       # noqa: E402
from aigineering.cli.verify import verify, readiness          # noqa: E402

cli.add_command(run)
cli.add_command(demo)
cli.add_command(trace)
cli.add_command(audit)
cli.add_command(replay)
cli.add_command(session)
cli.add_command(worker)
cli.add_command(retry)
cli.add_command(verify)
cli.add_command(readiness)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
