"""Read-only historical task assignment records."""

from __future__ import annotations

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import collaboration_service, require_redis
from aigineering.cli._common import (
    _cli_internal_error as _internal_error,
    _cli_result_failure as _failure,
    _cli_result_success as _success,
    _persistent_snapshot,
    _persistent_store,
)
from aigineering.core.domain import load_genesis
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_assignment import task_assignment_records


def _preflight(store, as_json: bool) -> None:
    try:
        load_genesis(store)
    except LookupError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    collaboration_service(store).sync()


def _require_redis(as_json: bool) -> None:
    try:
        require_redis()
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except Exception as exc:
        _internal_error(exc, as_json)


def _human_assignment(item: dict) -> str:
    worker = item["worker_id"] or "(unassigned)"
    return "\n".join(
        (
            f"Historical assignment {item['record_id']}",
            f"  Contract: {item['contract_id']}",
            f"  recorded Worker: {worker}",
            f"  previous assignment: {item['previous_assignment_id'] or '(none)'}",
            f"  reason: {item['reason']}",
            f"  actor/key: {item['actor_id']} / {item['key_id']}",
        )
    )


@click.command("assignments")
@click.argument("target")
@click.option("--json", "as_json", is_flag=True)
def task_assignments(target: str, as_json: bool) -> None:
    """Show historical assignment records; they do not affect claimability."""
    if not target.strip():
        _failure("invalid_input", "target Contract ID must be non-empty", as_json)
    _require_redis(as_json)
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    try:
        _preflight(snapshot, as_json)
    except (click.ClickException, click.exceptions.Exit):
        raise
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        snapshot.close()
    try:
        store = _persistent_store()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        data, revision = task_assignment_records(store, target)
    except RedisSyncPending as exc:
        _failure("redis_unavailable", str(exc), as_json)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except (OSError, RuntimeError) as exc:
        _failure("redis_unavailable", str(exc), as_json)
    finally:
        store.close()
    history = data["history"]
    detail = "\n\n".join(_human_assignment(item) for item in history)
    if not detail:
        detail = f"No assignment history for Contract {target}."
    _success(
        as_json,
        {
            "contract_id": target,
            "historical_worker_id": data.get("worker_id"),
            "history": history,
            "semantics": "Historical metadata only; it does not affect claimability.",
        },
        revision,
        "Historical assignment metadata only; it does not affect claimability.\n\n"
        + detail,
    )


def register_task_assignment_commands(task_group: click.Group) -> None:
    """Register the historical assignment reader."""
    task_group.add_command(task_assignments)


__all__ = ["register_task_assignment_commands"]
