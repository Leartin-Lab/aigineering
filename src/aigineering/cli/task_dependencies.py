"""Operator commands for signed task claim dependencies."""

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
from aigineering.task_dependencies import (
    publish_task_dependency_link,
    publish_task_dependency_unlink,
    task_dependency_records,
    validate_task_dependency_link,
    validate_task_dependency_unlink,
)
from aigineering.runtime_services import RedisSyncPending


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


def _preflight_link(
    target: str, prerequisite: str, reason: str, key: str | None, as_json: bool
) -> None:
    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        collaboration_service(snapshot).sync()
        validate_task_dependency_link(
            snapshot,
            target,
            prerequisite,
            reason,
            idempotency_key=key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    finally:
        snapshot.close()


def _preflight_unlink(
    target: str, link_id: str, reason: str, key: str | None, as_json: bool
) -> None:
    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        collaboration_service(snapshot).sync()
        validate_task_dependency_unlink(
            snapshot,
            target,
            link_id,
            reason,
            idempotency_key=key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    finally:
        snapshot.close()


def _human_record(item: dict) -> str:
    lines = [
        f"Dependency {item['constraint_id']}",
        f"  target: {item['contract_id']}",
        f"  prerequisite: {item['prerequisite_contract_id']}",
        f"  required terminal: {item['required_terminal']}",
        f"  link: {'ACTIVE' if item['active'] else 'RELEASED'}",
        f"  prerequisite terminal: {item.get('observed_terminal', 'unknown')}",
        f"  reason: {item['reason']}",
    ]
    for release in item.get("release_refs", ()):
        lines.append(f"  release {release['record_id']}: {release['reason']}")
    return "\n".join(lines)


def _human_history(items: list[dict], target: str) -> str:
    if not items:
        return f"No dependency links for Contract {target}."
    return "\n\n".join(_human_record(item) for item in items)


@click.command("link")
@click.argument("target")
@click.option(
    "--depends-on",
    "prerequisite",
    required=True,
    help="Exact prerequisite Contract ID.",
)
@click.option(
    "--reason", required=True, help="Why the target depends on this prerequisite."
)
@click.option("--idempotency-key", default=None, help="Signed Candidate replay key.")
@click.option("--json", "as_json", is_flag=True)
def task_link(
    target: str,
    prerequisite: str,
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Require one exact prerequisite to complete before a new task claim."""
    try:
        if not target.strip() or not prerequisite.strip():
            raise ValueError("target and prerequisite Contract IDs must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty text")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    _preflight_link(target, prerequisite, reason, idempotency_key, as_json)
    store = _persistent_store()
    try:
        item, revision = publish_task_dependency_link(
            store,
            target,
            prerequisite,
            reason,
            idempotency_key=idempotency_key,
        )
    except RedisSyncPending as exc:
        message = str(exc)
        if exc.accepted is True:
            message = f"{message} The dependency fact is committed and retained."
        _failure("redis_unavailable", message, as_json)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(
        as_json,
        item,
        revision,
        f"Linked {item['constraint_id']}: {target} waits for {prerequisite} ({reason})",
    )


@click.command("unlink")
@click.argument("target")
@click.option(
    "--link", "constraint_id", required=True, help="Exact dependency placement ID."
)
@click.option("--reason", required=True, help="Why this dependency is being released.")
@click.option("--idempotency-key", default=None, help="Signed Candidate replay key.")
@click.option("--json", "as_json", is_flag=True)
def task_unlink(
    target: str,
    constraint_id: str,
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Release one exact dependency placement without resetting task state."""
    try:
        if not target.strip() or not constraint_id.strip():
            raise ValueError("target and dependency placement IDs must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty text")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    _preflight_unlink(target, constraint_id, reason, idempotency_key, as_json)
    store = _persistent_store()
    try:
        item, revision = publish_task_dependency_unlink(
            store,
            target,
            constraint_id,
            reason,
            idempotency_key=idempotency_key,
        )
    except RedisSyncPending as exc:
        message = str(exc)
        if exc.accepted is True:
            message = f"{message} The release fact is committed and retained."
        _failure("redis_unavailable", message, as_json)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(
        as_json,
        item,
        revision,
        f"Released dependency {constraint_id} on {target}; fact {item['record_id']}: {reason}",
    )


@click.command("dependencies")
@click.argument("target")
@click.option("--json", "as_json", is_flag=True)
def task_dependencies(target: str, as_json: bool) -> None:
    """Show immutable dependency placements and release history for a task."""
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
    for command in (task_link, task_unlink, task_dependencies):
        task_group.add_command(copy(command))
