"""Read-only historical task priority records."""

from __future__ import annotations

import click

from aigineering.application import require_redis
from aigineering.cli._common import (
    _cli_result_failure as _error,
    _cli_result_success as _success,
    _persistent_snapshot,
)
from aigineering.core.domain import load_genesis
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_priority import task_priority_records


def _snapshot(as_json: bool):
    try:
        store = _persistent_snapshot()
        load_genesis(store)
        return store
    except (FileNotFoundError, LookupError):
        try:
            store.close()
        except (UnboundLocalError, AttributeError):
            pass
        _error("workspace_not_initialized", "Initialize this workspace first.", as_json)
        raise AssertionError("unreachable")


@click.command("priorities")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True)
def task_priorities(contract_id: str, as_json: bool) -> None:
    """Show historical priority records; priority does not reorder task execution."""
    try:
        require_redis()
        store = _snapshot(as_json)
        try:
            data, revision = task_priority_records(store, contract_id=contract_id)
        finally:
            store.close()
    except click.ClickException:
        raise
    except RedisSyncPending as exc:
        _error("redis_unavailable", str(exc), as_json)
    except LookupError as exc:
        _error("not_found", str(exc), as_json)
    except ValueError as exc:
        _error("invalid_input", str(exc), as_json)
    payload = {
        "contract_id": data["contract_id"],
        "historical_priority": data["priority"],
        "historical_head_id": data["head_priority_id"],
        "history": data["history"],
        "semantics": "Historical metadata only; it does not affect execution ordering or claimability.",
    }
    _success(as_json, payload, revision, _history_text(payload))


def _history_text(data: dict) -> str:
    lines = [
        f"Historical priority for {data['contract_id']}: {data['historical_priority']}",
        f"Historical head: {data['historical_head_id'] or '(no fact recorded)'}",
        "Historical metadata only; it does not affect execution ordering or claimability.",
    ]
    for row in data["history"]:
        lines.append(
            f"  {row['record_id']} priority={row['priority']} previous={row['previous_priority_id'] or '(none)'} reason={row['reason']}"
        )
        lines.append(
            f"    actor={row['actor_id']} key={row['key_id']} candidate={row['candidate_id']} revision={row['revision']} recorded_at={row['recorded_at']}"
        )
    return "\n".join(lines)


def register_task_priority_commands(task_group: click.Group) -> None:
    task_group.add_command(task_priorities)
