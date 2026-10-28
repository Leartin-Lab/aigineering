"""CLI adapters for read-only task attempt history."""

from __future__ import annotations

import click

from aigineering.application import collaboration_service
from aigineering.cli._common import _output_json, _persistent_snapshot
from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.redis_collaboration import RedisCollaborationUnavailable
from aigineering.task_diagnostics import (
    TaskDiagnosticError,
    task_diagnostics as read_task_diagnostics,
    task_runs as read_task_runs,
)


def register_task_diagnostic_commands(group: click.Group) -> None:
    """Register attempt history commands on the existing task group."""

    @group.command("runs")
    @click.argument("contract_id")
    @click.option("--limit", type=int, default=20, show_default=True)
    @click.option("--cursor", default=None)
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def task_runs(
        contract_id: str, limit: int, cursor: str | None, as_json: bool
    ) -> None:
        _run_inspection(
            lambda store: read_task_runs(
                store, contract_id, limit=limit, cursor=cursor
            ),
            as_json,
        )

    @group.command("diagnostics")
    @click.argument("contract_id")
    @click.option("--claim", "claim_id", required=True)
    @click.option("--limit", type=int, default=20, show_default=True)
    @click.option("--cursor", default=None)
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def task_diagnostics(
        contract_id: str,
        claim_id: str,
        limit: int,
        cursor: str | None,
        as_json: bool,
    ) -> None:
        _run_inspection(
            lambda store: read_task_diagnostics(
                store, contract_id, claim_id, limit=limit, cursor=cursor
            ),
            as_json,
        )


def _run_inspection(operation, as_json: bool) -> None:
    store = None
    try:
        store = _persistent_snapshot()
        # Inspection keeps the operational Redis requirement. The immutable
        # SQLite snapshot remains the history authority.
        collaboration_service(store).sync()
        result = operation(store)
    except (RedisCollaborationUnavailable, QueryProjectionUnavailable) as error:
        _emit_error("redis_unavailable", as_json, type(error).__name__)
        return
    except TaskDiagnosticError as error:
        _emit_error(error.code, as_json, None)
        return
    except Exception as error:
        _emit_error("inspection_failed", as_json, type(error).__name__)
        return
    finally:
        if store is not None:
            store.close()
    if as_json:
        _output_json(result)
    else:
        data = result["data"]
        rows = data.get("runs", data.get("events", []))
        click.echo(
            f"{data['contract_id']} {len(rows)} "
            f"{'claim attempts' if 'runs' in data else 'diagnostic events'} "
            f"(revision {data['watermark']})"
        )
        for row in rows:
            if "grant_record_id" in row:
                click.echo(
                    f"{row.get('claim_id')} {row.get('status')} "
                    f"worker={row.get('worker_id')} revision={row.get('grant_revision')}"
                )
            else:
                click.echo(
                    f"{row.get('revision')} {row.get('record_type')} {row.get('record_id')}"
                )
        if data["next_cursor"]:
            click.echo(f"next_cursor: {data['next_cursor']}")
        if not result["ok"]:
            click.echo("integrity: inconsistent")
    if not result["ok"]:
        raise click.exceptions.Exit(1)


def _emit_error(code: str, as_json: bool, error_type: str | None) -> None:
    payload = {"ok": False, "error": code}
    if error_type is not None:
        payload["error_type"] = error_type
    if as_json:
        _output_json(payload)
    else:
        click.echo(f"Task inspection failed ({code})", err=True)
    raise click.exceptions.Exit(1)
