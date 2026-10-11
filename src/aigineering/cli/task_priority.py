"""Operator commands for signed advisory task priority."""

from __future__ import annotations

from copy import copy
import re

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import collaboration_service, require_redis
from aigineering.cli._common import (
    _output_json,
    _persistent_snapshot,
    _persistent_store,
)
from aigineering.cli._task_batch import (
    collect_task_batch,
    emit_batch,
    new_batch_key_base,
    preflight_task_batch,
    target_key,
    validate_batch_key,
    validate_task_ids,
)
from aigineering.core.domain import load_genesis
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_priority import (
    PriorityPublicationRejected,
    publish_task_priority,
    task_priority_records,
    validate_task_priority,
)
from aigineering.plugins.task_priority import PriorityStateRejected


def _error(code: str, message: str, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": {"code": code, "message": message}})
        raise click.exceptions.Exit(1)
    raise click.ClickException(message)


def _pending_failure(exc: RedisSyncPending, as_json: bool) -> None:
    facts_retained = exc.accepted is True or (
        exc.accepted is False and exc.decision is not None
    )
    if as_json:
        payload = {
            "ok": False,
            "error": "redis_sync_pending"
            if exc.accepted is True or exc.accepted is False
            else "redis_unavailable",
            "message": "Required Redis synchronization is pending.",
            "candidate_accepted": exc.accepted,
            "facts_retained": facts_retained,
        }
        if exc.data is not None:
            payload["data"] = exc.data
        if exc.revision is not None:
            payload["revision"] = exc.revision
        _output_json(payload)
        raise click.exceptions.Exit(1) from exc
    if exc.accepted is True and isinstance(exc.data, dict):
        key = exc.data.get("idempotency_key")
        record_id = exc.data.get("record_id")
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9._:-]+", key):
            suffix = f" Replay with --idempotency-key {key}."
        else:
            suffix = (
                " Replay with the original --idempotency-key value; "
                "use --json to retrieve the exact key."
            )
        click.echo(
            f"Redis synchronization is pending; committed fact {record_id}.{suffix}",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc
    raise click.ClickException("Required Redis synchronization is pending.") from exc


def _internal_error(exc: Exception, as_json: bool) -> None:
    message = "Unexpected internal task command error."
    if as_json:
        _output_json(
            {"ok": False, "error": {"code": "internal_error", "message": message}}
        )
        raise click.exceptions.Exit(1) from exc
    raise click.ClickException(message) from exc


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


@click.command("priority", context_settings={"ignore_unknown_options": True})
@click.argument("contract_ids", nargs=-1, required=True)
@click.argument("priority")
@click.option("--reason", required=True)
@click.option("--previous", "previous_priority_id", default=None)
@click.option("--previous-for", "previous_for", multiple=True)
@click.option("--idempotency-key", default=None)
@click.option("--json", "as_json", is_flag=True)
def task_priority(
    contract_ids: tuple[str, ...],
    priority: str,
    reason: str,
    previous_priority_id: str | None,
    previous_for: tuple[str, ...],
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Set advisory scheduling priority (-100 low through 100 high; 0 clears)."""
    if len(contract_ids) == 1 and not previous_for:
        _task_priority_single(
            contract_ids[0],
            priority,
            reason,
            previous_priority_id,
            idempotency_key,
            as_json,
        )
        return
    if len(contract_ids) < 2:
        _error(
            "invalid_input",
            "A priority predecessor map is available only for multi-task calls.",
            as_json,
        )
    _task_priority_batch(
        contract_ids,
        priority,
        reason,
        previous_priority_id,
        previous_for,
        idempotency_key,
        as_json,
    )


# The bulk callback must validate every target and mapping before Redis access.
# Both execution branches perform their own required Redis preflight.
task_priority.callback._redis_protected = True


def _task_priority_single(
    contract_id: str,
    priority: str,
    reason: str,
    previous_priority_id: str | None,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    try:
        if not re.fullmatch(r"[+-]?[0-9]+", priority):
            raise ValueError("priority must be an integer from -100 through 100")
        priority_value = int(priority)
        require_redis()
        store = _snapshot(as_json)
        try:
            collaboration_service(store).sync()
            validate_task_priority(
                store,
                contract_id,
                priority_value,
                reason,
                previous_priority_id=previous_priority_id,
                idempotency_key=idempotency_key,
            )
        finally:
            store.close()
    except click.ClickException:
        raise
    except click.exceptions.Exit:
        raise
    except RedisSyncPending as exc:
        _pending_failure(exc, as_json)
    except QueryProjectionUnavailable:
        _error(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except LookupError as exc:
        _error("not_found", str(exc), as_json)
    except ValueError as exc:
        _error("invalid_input", str(exc), as_json)
    except Exception as exc:
        _internal_error(exc, as_json)

    try:
        store = _persistent_store()
    except FileNotFoundError:
        _error("workspace_not_initialized", "Initialize this workspace first.", as_json)
    except click.ClickException:
        raise
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        item, revision = publish_task_priority(
            store,
            contract_id,
            priority_value,
            reason,
            previous_priority_id=previous_priority_id,
            idempotency_key=idempotency_key,
        )
    except RedisSyncPending as exc:
        _pending_failure(exc, as_json)
    except QueryProjectionUnavailable:
        _error(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except LookupError as exc:
        _error("not_found", str(exc), as_json)
    except ValueError as exc:
        _error("rejected", str(exc), as_json)
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        store.close()
    _success(
        as_json,
        item,
        revision,
        f"Priority for {contract_id} is {item['priority']} (fact {item['record_id']}).",
    )


def _task_priority_batch(
    contract_ids: tuple[str, ...],
    priority: str,
    reason: str,
    previous_priority_id: str | None,
    previous_for: tuple[str, ...],
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    try:
        ids = validate_task_ids(contract_ids)
        if previous_priority_id is not None:
            raise ValueError("multi-task priority uses --previous-for, not --previous")
        if not re.fullmatch(r"[+-]?[0-9]+", priority):
            raise ValueError("priority must be an integer from -100 through 100")
        priority_value = int(priority)
        if not -100 <= priority_value <= 100:
            raise ValueError("priority must be an integer from -100 through 100")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must not be empty or whitespace")
        if len(reason.encode("utf-8", errors="strict")) > 65536:
            raise ValueError("reason exceeds 65536 UTF-8 bytes")
        previous_map = _priority_previous_mapping(previous_for, ids)
        validate_batch_key(idempotency_key)
        if (
            idempotency_key is not None
            and len(idempotency_key.encode("utf-8", errors="strict")) > 65536
        ):
            raise ValueError("idempotency key exceeds 65536 UTF-8 bytes")
    except (UnicodeEncodeError, ValueError) as exc:
        _error("invalid_input", str(exc), as_json)
        return

    base = idempotency_key or new_batch_key_base("priority")
    try:
        preflight_task_batch(_persistent_snapshot)
    except FileNotFoundError:
        _error("workspace_not_initialized", "Initialize this workspace first.", as_json)
        return
    except (click.ClickException, click.exceptions.Exit):
        raise
    except (RedisSyncPending, QueryProjectionUnavailable):
        _error(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
        return
    except Exception as exc:
        _internal_error(exc, as_json)
        return

    def invoke(contract_id: str, key: str | None) -> tuple[dict, int]:
        snapshot = _persistent_snapshot()
        try:
            load_genesis(snapshot)
            collaboration_service(snapshot).sync()
            try:
                validate_task_priority(
                    snapshot,
                    contract_id,
                    priority_value,
                    reason,
                    previous_priority_id=previous_map[contract_id],
                    idempotency_key=key,
                )
            except PriorityStateRejected as exc:
                exc.revision = snapshot.get_runtime_revision()
                raise
        finally:
            snapshot.close()
        store = _persistent_store()
        try:
            return publish_task_priority(
                store,
                contract_id,
                priority_value,
                reason,
                previous_priority_id=previous_map[contract_id],
                idempotency_key=key,
            )
        finally:
            store.close()

    collection = collect_task_batch(
        ids,
        action="priority",
        batch_key_base=base,
        key_for_target=lambda contract_id: target_key("priority", base, contract_id),
        invoke=invoke,
        expected_rejection_types=(PriorityStateRejected, PriorityPublicationRejected),
        rejection_result=_priority_rejection_result,
        include_operation_revision=True,
    )
    emit_batch(
        collection,
        as_json=as_json,
        emit_json=_output_json,
        safe_human_base=True,
    )


def _priority_rejection_result(exc: Exception):
    if isinstance(exc, PriorityPublicationRejected):
        return exc.data, exc.revision
    if isinstance(exc, PriorityStateRejected):
        return None, getattr(exc, "revision", None)
    return None, None


def _priority_previous_mapping(values: tuple[str, ...], contract_ids: tuple[str, ...]):
    """Parse a complete per-task exact head map; an empty value means no history."""
    targets = set(contract_ids)
    mapped: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str):
            raise ValueError("priority predecessor mapping must be text")
        target, separator, head = value.partition("=")
        if not separator or not target.strip():
            raise ValueError("priority predecessor must be TASK_ID=FACT_ID")
        try:
            target.encode("utf-8", errors="strict")
            head.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "priority predecessor mapping must be valid UTF-8"
            ) from exc
        if target not in targets:
            raise ValueError(
                f"priority predecessor references unselected Contract {target!r}"
            )
        if target in mapped:
            raise ValueError(f"Contract {target!r} has duplicate priority predecessors")
        if head.strip() != head:
            raise ValueError(
                "priority predecessor IDs must not contain surrounding whitespace"
            )
        mapped[target] = head
    missing = [contract_id for contract_id in contract_ids if contract_id not in mapped]
    if missing:
        raise ValueError(
            f"every task needs an exact priority predecessor: {', '.join(missing)}"
        )
    return mapped


@click.command("priorities")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True)
def task_priorities(contract_id: str, as_json: bool) -> None:
    """Show the exact priority head and immutable history for a task."""
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
    _success(as_json, data, revision, _history_text(data))


def _success(as_json: bool, data: object, revision: int, human: str) -> None:
    if as_json:
        _output_json({"ok": True, "data": data, "revision": revision})
    else:
        click.echo(human)


def _history_text(data: dict) -> str:
    lines = [
        f"Task {data['contract_id']} priority: {data['priority']}",
        f"Head: {data['head_priority_id'] or '(default; no fact)'}",
    ]
    for row in data["history"]:
        lines.append(
            f"  {row['record_id']} priority={row['priority']} previous={row['previous_priority_id'] or '(none)'} reason={row['reason']}"
        )
        lines.append(
            f"    actor={row['actor_id']} key={row['key_id']} candidate={row['candidate_id']} revision={row['revision']} recorded_at={row['recorded_at']}"
        )
    if data["head_priority_id"]:
        lines.append(
            f"Set again with: aig task priority {data['contract_id']} VALUE --previous {data['head_priority_id']} --reason '...'"
        )
    return "\n".join(lines)


def register_task_priority_commands(task_group: click.Group) -> None:
    task_group.add_command(copy(task_priority))
    task_group.add_command(copy(task_priorities))
