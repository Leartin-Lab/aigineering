"""Operator commands for signed review-linked task revisions."""

from __future__ import annotations


import click

from aigineering.application import collaboration_service, require_redis
from aigineering.cli._common import (
    _cli_result_failure as _failure,
    _cli_result_success as _success,
    _persistent_snapshot,
)
from aigineering.core.domain import load_genesis
from aigineering.task_revision import (
    task_revision_records,
)


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
    task_group.add_command(task_revisions)
