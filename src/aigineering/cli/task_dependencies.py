"""Operator commands for signed task claim dependencies."""

from __future__ import annotations


import click

from aigineering.application import require_redis
from aigineering.cli._common import (
    _cli_result_failure as _failure,
    _cli_result_success as _success,
    _persistent_snapshot,
)
from aigineering.core.domain import load_genesis
from aigineering.task_dependencies import (
    task_dependency_records,
)


def _open_snapshot(as_json: bool):
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable")
    try:
        load_genesis(snapshot)
    except LookupError:
        snapshot.close()
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable")
    return snapshot


def _human_record(item: dict) -> str:
    lines = [
        f"Historical dependency record {item['constraint_id']}",
        f"  target: {item['contract_id']}",
        f"  prerequisite: {item['prerequisite_contract_id']}",
        f"  recorded required terminal: {item['required_terminal']}",
        f"  recorded link state: {'active' if item['active'] else 'released'}",
        f"  recorded prerequisite terminal: {item.get('observed_terminal', 'unknown')}",
        f"  reason: {item['reason']}",
    ]
    for release in item.get("release_refs", ()):
        lines.append(f"  release {release['record_id']}: {release['reason']}")
    return "\n".join(lines)


def _human_history(items: list[dict], target: str) -> str:
    if not items:
        return f"No dependency links for Contract {target}."
    return "\n\n".join(_human_record(item) for item in items)


@click.command("dependencies")
@click.argument("target")
@click.option("--json", "as_json", is_flag=True)
def task_dependencies(target: str, as_json: bool) -> None:
    """Show historical dependency records; they do not gate current claims."""
    if not target.strip():
        _failure("invalid_input", "target Contract ID must be non-empty", as_json)
    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        items, revision = task_dependency_records(snapshot, contract_id=target)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    finally:
        snapshot.close()
    _success(as_json, items, revision, _human_history(items, target))


def register_task_dependency_commands(task_group: click.Group) -> None:
    task_group.add_command(task_dependencies)
