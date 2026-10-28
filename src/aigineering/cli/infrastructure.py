"""Shared operational preflight for public CLI leaf commands."""

from __future__ import annotations

from functools import wraps

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import require_redis
from aigineering.cli._common import _output_json
from aigineering.runtime_services import RedisSyncPending

_OFFLINE_READS = {
    ("task", "status"),
    ("task", "audit"),
    ("trace",),
    ("audit",),
    ("session", "show"),
    ("session", "ls"),
    ("domain", "show"),
}


def protect_commands(group: click.Group, path: tuple[str, ...] = ()) -> None:
    """Preflight after Click parses leaf options, so help needs no infrastructure."""
    for name, command in group.commands.items():
        command_path = (*path, name)
        if isinstance(command, click.Group):
            protect_commands(command, command_path)
            continue
        original = command.callback
        if original is None or getattr(original, "_redis_protected", False):
            continue

        def wrap(callback, operation):
            @wraps(callback)
            def invoke(*args, **kwargs):
                ctx = click.get_current_context()
                offline = bool((ctx.find_root().obj or {}).get("offline"))
                as_json = bool(
                    kwargs.get("as_json")
                    or kwargs.get("json_output")
                    or operation == ("worker", "submit")
                )
                if offline and operation not in _OFFLINE_READS:
                    raise click.UsageError(
                        "--offline permits only task status/audit, trace/audit, "
                        "session show/ls and domain show; execution and mutation require Redis."
                    )
                try:
                    if not offline and operation[0] not in {"redis", "cache"}:
                        require_redis()
                    return callback(*args, **kwargs)
                except QueryProjectionUnavailable as exc:
                    payload = {
                        "ok": False,
                        "error": "redis_sync_pending"
                        if isinstance(exc, RedisSyncPending)
                        else "redis_unavailable",
                        "message": str(exc),
                    }
                    if isinstance(exc, RedisSyncPending):
                        payload["facts_retained"] = True
                        payload["candidate_accepted"] = exc.accepted
                    if as_json:
                        _output_json(payload)
                        raise click.exceptions.Exit(1) from exc
                    raise click.ClickException(str(exc)) from exc
                except FileNotFoundError as exc:
                    message = (
                        "Workspace Store missing; initialize with 'aig domain init'."
                    )
                    if as_json:
                        _output_json(
                            {
                                "ok": False,
                                "error": "workspace_uninitialized",
                                "message": message,
                            }
                        )
                        raise click.exceptions.Exit(1) from exc
                    raise click.ClickException(message) from exc

            invoke._redis_protected = True
            return invoke

        command.callback = wrap(original, command_path)
