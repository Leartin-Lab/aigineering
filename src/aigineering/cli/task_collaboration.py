"""Signed task collaboration commands."""

from __future__ import annotations

import click
from copy import copy

from aigineering.application import require_redis
from aigineering.cli._common import (
    _cli_result_failure as _failure,
    _cli_result_success as _success,
    _persistent_snapshot,
    _persistent_store,
)
from aigineering.core.domain import load_genesis
from aigineering.task_collaboration import (
    task_hold_records,
    task_promotion_records,
    task_comment_records,
)


def _human_holds(items: list[dict], contract_id: str) -> str:
    if not items:
        return f"No holds for Contract {contract_id}."
    lines = [f"Holds for Contract {contract_id}:"]
    for item in items:
        state = "recorded active" if item["active"] else "recorded released"
        lines.append(f"Historical {state} hold {item['hold_id']} — {item['reason']}")
        for release in item["release_refs"]:
            lines.append(f"  release {release['record_id']} — {release['reason']}")
    return "\n".join(lines)


@click.command("comments")
@click.argument("contract_id")
@click.option("--after", type=click.IntRange(min=0), default=0, show_default=True)
@click.option("--limit", type=click.IntRange(min=1, max=1000), default=100)
@click.option("--json", "as_json", is_flag=True)
def task_comments(contract_id: str, after: int, limit: int, as_json: bool) -> None:
    """List durable comments for one Contract after a Store revision."""
    if not contract_id.strip():
        _failure("invalid_input", "contract_id must be a non-empty string", as_json)
    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        try:
            load_genesis(snapshot)
        except LookupError as exc:
            _failure(
                "workspace_not_initialized", "Initialize this workspace first.", as_json
            )
            raise AssertionError("unreachable") from exc
        comments, revision = task_comment_records(snapshot, contract_id=contract_id)
        if snapshot.get_contract(contract_id) is None:
            _failure("not_found", f"unknown Contract {contract_id!r}", as_json)
        selected = [item for item in comments if item["revision"] > after][:limit]
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    finally:
        snapshot.close()
    lines = [f"Comments for Contract {contract_id}:"]
    lines.extend(f"{item['record_id']} — {item['text']}" for item in selected)
    _success(
        as_json,
        selected,
        revision,
        "\n".join(lines) if selected else f"No comments for Contract {contract_id}.",
    )


@click.command("holds")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True)
def task_holds(contract_id: str, as_json: bool) -> None:
    """List historical hold placements and release records for one task."""
    if not contract_id.strip():
        _failure("invalid_input", "contract_id must be a non-empty string", as_json)
    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        try:
            load_genesis(snapshot)
        except LookupError as exc:
            _failure(
                "workspace_not_initialized", "Initialize this workspace first.", as_json
            )
            raise AssertionError("unreachable") from exc
        holds, revision = task_hold_records(snapshot, contract_id=contract_id)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    finally:
        snapshot.close()
    _success(as_json, holds, revision, _human_holds(holds, contract_id))


@click.command("promotions")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True)
def task_promotions(contract_id: str, as_json: bool) -> None:
    """List immutable promotion audit facts for one task."""
    if not contract_id.strip():
        _failure("invalid_input", "contract_id must be non-empty", as_json)
    require_redis()
    store = _persistent_store()
    try:
        history, revision = task_promotion_records(store, contract_id=contract_id)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    finally:
        store.close()
    payload = {"contract_id": contract_id, "history": history}
    human = (
        "\n".join(
            f"{item['record_id']} — released {', '.join(item['hold_ids'])}: {item['reason']}"
            for item in history
        )
        if history
        else f"No promotion history for Contract {contract_id}."
    )
    _success(as_json, payload, revision, human)


def _open_snapshot(as_json: bool):
    try:
        return _persistent_snapshot()
    except FileNotFoundError as exc:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable") from exc


def register_task_collaboration_commands(task_group: click.Group) -> None:
    for command in (
        task_comments,
        task_holds,
        task_promotions,
    ):
        task_group.add_command(copy(command))
