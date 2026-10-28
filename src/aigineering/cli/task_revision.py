"""Operator commands for signed review-linked task revisions."""

from __future__ import annotations

from copy import copy

import click

from aigineering.application import collaboration_service, require_redis
from aigineering.cli._common import (
    _output_json,
    _persistent_snapshot,
    _persistent_store,
)
from aigineering.core.domain import load_genesis
from aigineering.task_revision import (
    publish_task_revision,
    task_revision_records,
    validate_task_revision,
)


def _failure(code: str, message: str, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": {"code": code, "message": message}})
        raise click.exceptions.Exit(1)
    raise click.ClickException(message)


def _success(as_json: bool, data: object, revision: int, human: str) -> None:
    if as_json:
        _output_json({"ok": True, "data": data, "revision": revision})
    else:
        click.echo(human)


def _snapshot(as_json: bool):
    try:
        return _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable")


def _preflight(snapshot, as_json: bool) -> None:
    try:
        load_genesis(snapshot)
    except LookupError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    collaboration_service(snapshot).sync()


def _human_revision(item: dict) -> str:
    successor = item["successor_id"]
    lines = [
        f"Revision {item['record_id']}",
        f"  source: {item['contract_id']}",
        f"  successor: {successor}",
        f"  review decision: {item['decision_id']}",
        f"  report: {item['report_asset_id']}",
        f"  budget: {item['budget']}",
        f"  reason: {item['reason']}",
        f"  continue with: aig run --task {successor}",
    ]
    if "assignment_mode" in item:
        lines.extend(
            (
                f"  successor assignment: {item['assignment_mode']} to "
                f"{item.get('worker_id') or '(unassigned)'}",
                f"  source assignment: {item.get('source_assignment_id') or '(none)'}",
                "  successor assignment fact: "
                f"{item.get('assignment_fact_id') or '(none emitted)'}",
            )
        )
    return "\n".join(lines)


def _human_history(items: list[dict], source: str) -> str:
    if not items:
        return f"No revisions from Contract {source}."
    return "\n\n".join(_human_revision(item) for item in items)


@click.command("request-changes")
@click.argument("decision_id")
@click.option("--reason", required=True, help="Why bounded revision work is needed.")
@click.option(
    "--budget",
    type=click.IntRange(min=1),
    default=None,
    help="Successor allowance; omitted keeps the signed request null and resolves at first projection.",
)
@click.option(
    "--assignment-worker",
    default=None,
    help="Explicitly assign the successor to this registered Worker.",
)
@click.option(
    "--clear-assignment",
    is_flag=True,
    help="Explicitly leave the successor unassigned.",
)
@click.option(
    "--source-assignment-id",
    default=None,
    help="Exact source assignment head required for an override; use an empty value only when no head exists.",
)
@click.option("--idempotency-key", default=None, help="Signed Candidate replay key.")
@click.option("--json", "as_json", is_flag=True)
def task_request_changes(
    decision_id: str,
    reason: str,
    budget: int | None,
    assignment_worker: str | None,
    clear_assignment: bool,
    source_assignment_id: str | None,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Publish a bounded successor for an exact review decision marked changes."""
    try:
        if not decision_id.strip():
            raise ValueError("decision ID must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty text")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
        override_requested = assignment_worker is not None or clear_assignment
        if override_requested != (source_assignment_id is not None):
            raise ValueError("assignment override requires --source-assignment-id")
        if clear_assignment == (assignment_worker is not None):
            if override_requested:
                raise ValueError(
                    "choose exactly one of --assignment-worker or --clear-assignment"
                )
        if assignment_worker is not None and not assignment_worker.strip():
            raise ValueError("assignment Worker ID must be non-empty")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)

    require_redis()
    snapshot = _snapshot(as_json)
    try:
        _preflight(snapshot, as_json)
        validate_task_revision(
            snapshot,
            decision_id,
            reason,
            budget=budget,
            assignment_worker_id=assignment_worker,
            clear_assignment=clear_assignment,
            source_assignment_id=source_assignment_id,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    finally:
        snapshot.close()

    store = _persistent_store()
    try:
        data, revision = publish_task_revision(
            store,
            decision_id,
            reason,
            budget=budget,
            assignment_worker_id=assignment_worker,
            clear_assignment=clear_assignment,
            source_assignment_id=source_assignment_id,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(as_json, data, revision, _human_revision(data))


@click.command("revisions")
@click.argument("source")
@click.option("--json", "as_json", is_flag=True)
def task_revisions(source: str, as_json: bool) -> None:
    """List immutable revision links published from one source task."""
    if not source.strip():
        _failure("invalid_input", "source Contract ID must be non-empty", as_json)
    require_redis()
    snapshot = _snapshot(as_json)
    try:
        _preflight(snapshot, as_json)
        items, revision = task_revision_records(snapshot, source)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    finally:
        snapshot.close()
    _success(as_json, items, revision, _human_history(items, source))


def register_task_revision_commands(task_group: click.Group) -> None:
    for command in (task_request_changes, task_revisions):
        task_group.add_command(copy(command))
