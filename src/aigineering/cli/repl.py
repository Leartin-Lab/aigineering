"""aig repl — interactive read-eval-print loop for the Aigineering runtime."""

from __future__ import annotations

import shlex

import click


@click.command("repl")
def repl() -> None:
    """Start an interactive REPL session.

    Supported commands (prefix with /):

    \b
      /asset add --name X --content Y   Inject a control-plane asset
      /contract add --name X --input Y   Inject a contract
      /mcp add --name X --source-uri U  Add an MCP descriptor
      /run <goal>                       Execute a demo run
      /trace                            Show the latest trace
      /quit                             Exit the REPL
    """
    click.echo("aig REPL — type /quit to exit, /help for commands")
    _repl_loop()


def _repl_loop() -> None:
    """Run the REPL main loop."""
    while True:
        try:
            raw = input("aig> ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo()
            break

        if not raw:
            continue

        if not raw.startswith("/"):
            click.echo("Commands must start with /. Type /help for available commands.")
            continue

        parts = raw[1:].split(None, 1)
        command = parts[0].lower()
        args_str = parts[1] if len(parts) > 1 else ""

        try:
            argv = shlex.split(args_str) if args_str else []
        except ValueError as e:
            click.echo(f"Parse error: {e}")
            continue

        if command == "quit":
            click.echo("Goodbye.")
            break
        elif command == "help":
            _show_repl_help()
        elif command == "asset":
            _dispatch(["asset"] + argv)
        elif command == "contract":
            _dispatch(["contract"] + argv)
        elif command == "run":
            _dispatch(["run"] + argv)
        elif command == "trace":
            _dispatch(["trace"] + argv)
        elif command == "behavior":
            _dispatch(["behavior"] + argv)
        elif command == "mcp":
            _dispatch(["mcp"] + argv)
        else:
            click.echo(f"Unknown command: /{command}. Type /help for available commands.")


def _dispatch(cli_args: list[str]) -> None:
    """Dispatch a CLI invocation through the main Click group."""
    from aigineering.cli.main import cli as main_cli

    try:
        result = main_cli(cli_args, standalone_mode=False)
        if result is not None:
            click.echo(str(result))
    except SystemExit:
        pass
    except Exception as e:
        click.echo(f"Error: {e}")


def _show_repl_help() -> None:
    """Print REPL help text."""
    click.echo("Available commands:")
    click.echo("  /asset add --name X --content Y   Inject an asset")
    click.echo("  /contract add --name X [options]  Inject a contract")
    click.echo("  /behavior add --name X --file F   Add a behaviour asset")
    click.echo("  /behavior list                    List behaviour assets")
    click.echo("  /behavior show <name>             Show a behaviour asset")
    click.echo("  /mcp add --name X --source-uri U  Add an MCP descriptor")
    click.echo("  /mcp list                         List MCP descriptors")
    click.echo("  /mcp show <name>                  Show an MCP descriptor")
    click.echo("  /run <goal> [options]             Execute a demo run")
    click.echo("  /trace [options]                  Show the latest trace")
    click.echo("  /quit                             Exit the REPL")
    click.echo("  /help                             Show this help")
