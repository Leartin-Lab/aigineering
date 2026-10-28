"""CLI for read-only local Worker operational log inspection."""

from __future__ import annotations

import json
import math
from pathlib import Path
import time

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import collaboration_service
from aigineering.cli._common import _output_json
from aigineering.redis_collaboration import RedisCollaborationUnavailable
from aigineering.task_logs import (
    TaskLogError,
    read_capture,
    store_log_root,
)
from aigineering.task_diagnostics import TaskDiagnosticError, task_log_binding


def register_task_log_command(group: click.Group) -> None:
    """Register bounded read/follow of one exact signed claim's local capture."""

    @group.command("logs")
    @click.argument("contract_id")
    @click.option("--claim", "claim_id", required=True)
    @click.option(
        "--store",
        "store_path",
        type=click.Path(path_type=Path),
        default=Path(".aig/store.db"),
        show_default=True,
    )
    @click.option("--limit", type=click.IntRange(1, 100), default=20, show_default=True)
    @click.option("--cursor", default=None)
    @click.option(
        "--follow",
        is_flag=True,
        help="Poll this exact capture until timeout or interrupted.",
    )
    @click.option("--timeout", type=float, default=60.0, show_default=True)
    @click.option("--json", "as_json", is_flag=True, help="Output JSON.")
    def task_logs(
        contract_id, claim_id, store_path, limit, cursor, follow, timeout, as_json
    ):
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
            raise click.BadParameter(
                "must be finite and between 0 and 3600 seconds", param_hint="--timeout"
            )
        if not follow:
            result = _read_once(contract_id, claim_id, store_path, limit, cursor)
            _emit_result(result, as_json)
            return

        deadline = time.monotonic() + timeout
        active_cursor = cursor
        emitted = 0
        ever_available = False
        status = "timeout"
        try:
            while time.monotonic() < deadline:
                result = _read_once(
                    contract_id, claim_id, store_path, limit, active_cursor
                )
                if not result["ok"]:
                    if result.get("error") == "capture_missing":
                        if ever_available:
                            _emit_follow_error(result, as_json)
                        time.sleep(0.2)
                        continue
                    _emit_follow_error(result, as_json)
                    return
                ever_available = True
                data = result["data"]
                for event in data["events"]:
                    if as_json:
                        _output_json_line({"ok": True, "type": "event", "data": event})
                    else:
                        _human_event(event)
                emitted += len(data["events"])
                active_cursor = data["next_cursor"]
                if emitted and data["truncated"]:
                    continue
                time.sleep(0.2)
            status = "timeout"
        except KeyboardInterrupt:
            status = "interrupted"
        if not ever_available:
            _emit_follow_error({"error": "capture_missing"}, as_json)
        end = {
            "ok": True,
            "type": "end",
            "status": status,
            "next_cursor": active_cursor,
        }
        if as_json:
            _output_json_line(end)
        else:
            click.echo(f"log follow {status}")
            if active_cursor:
                click.echo(f"next_cursor: {active_cursor}")


def _read_once(contract_id, claim_id, store_path, limit, cursor):
    from aigineering.core.sqlite_store import SQLiteStore

    store = None
    try:
        store = SQLiteStore.snapshot_from_path(store_path)
        collaboration_service(store).sync()
        binding = task_log_binding(store, contract_id, claim_id)
        return read_capture(
            store_log_root(store_path), binding, limit=limit, cursor=cursor
        )
    except TaskLogError as error:
        return {"ok": False, "error": error.code}
    except PermissionError:
        return {"ok": False, "error": "capture_unavailable"}
    except TaskDiagnosticError as error:
        code = error.code
        if code == "task_not_found":
            code = "contract_not_found"
        if code not in {
            "invalid_claim_id",
            "claim_not_found",
            "claim_scope_mismatch",
            "claim_integrity_error",
            "claim_binding_unavailable",
            "contract_not_found",
        }:
            code = "inspection_failed"
        return {"ok": False, "error": code}
    except (RedisCollaborationUnavailable, QueryProjectionUnavailable):
        return {"ok": False, "error": "redis_unavailable"}
    except Exception:
        return {"ok": False, "error": "inspection_failed"}
    finally:
        if store is not None:
            store.close()


def _emit_result(result, as_json):
    if not result.get("ok"):
        _emit_error(result.get("error", "inspection_failed"), as_json)
        return
    if as_json:
        _output_json(result)
        return
    data = result["data"]
    click.echo(
        f"{data['contract_id']} claim {data['claim_id']}: {len(data['events'])} captured events"
    )
    for event in data["events"]:
        _human_event(event)
    click.echo(f"capture {data['capture_id']} ({data['follow_status']})")
    if data["next_cursor"]:
        click.echo(f"next_cursor: {data['next_cursor']}")


def _emit_follow_error(result, as_json):
    code = result.get("error", "inspection_failed")
    if as_json:
        _output_json_line(
            {"ok": False, "error": code, "type": "end", "status": "error"}
        )
        raise click.exceptions.Exit(1)
    _emit_error(code, as_json)


def _emit_error(code, as_json):
    payload = {"ok": False, "error": code}
    if as_json:
        _output_json(payload)
    else:
        click.echo(f"Task logs unavailable ({code})", err=True)
    raise click.exceptions.Exit(1)


def _output_json_line(payload):
    click.echo(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _human_event(event):
    fields = event.get("fields", {})
    # Event names and values are schema-limited scalars; JSON escaping keeps
    # terminal control characters from becoming forged human-readable lines.
    click.echo(
        json.dumps(
            {
                "sequence": event.get("sequence"),
                "event": event.get("event"),
                "recorded_at": event.get("recorded_at"),
                "fields": fields,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
