"""Signed task collaboration commands."""

from __future__ import annotations

import click
from copy import copy

from aigineering.application import collaboration_service, require_redis
from aigineering.cli._common import (
    _output_json,
    _persistent_snapshot,
    _persistent_store,
)
from aigineering.core.domain import load_genesis
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_collaboration import (
    publish_task_block,
    publish_task_comment,
    publish_task_unblock,
    task_hold_records,
    publish_task_promote,
    task_promotion_records,
    task_comment_records,
    validate_comment_target,
    validate_comment_text,
    validate_task_block,
    validate_task_unblock,
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


def _pending_failure(exc: RedisSyncPending, as_json: bool) -> None:
    if as_json:
        _output_json(
            {
                "ok": False,
                "error": "redis_sync_pending",
                "message": str(exc),
                "facts_retained": True,
                "candidate_accepted": exc.accepted,
                "data": exc.data,
                "revision": exc.revision,
            }
        )
        raise click.exceptions.Exit(1) from exc
    raise click.ClickException(str(exc)) from exc


def _task_preflight(as_json: bool) -> None:
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
        collaboration_service(snapshot).sync()
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    finally:
        snapshot.close()


def _human_comment(item: dict) -> str:
    return (
        f"Comment {item['record_id']} (Contract {item['contract_id']})\n"
        f"{item['text']}\nRecorded: {item['recorded_at']}"
    )


def _human_holds(items: list[dict], contract_id: str) -> str:
    if not items:
        return f"No holds for Contract {contract_id}."
    lines = [f"Holds for Contract {contract_id}:"]
    for item in items:
        state = "ACTIVE" if item["active"] else "RELEASED"
        lines.append(f"{state} hold {item['hold_id']} — {item['reason']}")
        for release in item["release_refs"]:
            lines.append(f"  release {release['record_id']} — {release['reason']}")
    return "\n".join(lines)


@click.command("comment")
@click.argument("contract_id")
@click.argument("text")
@click.option("--reply-to", default="", help="Committed comment record ID.")
@click.option(
    "--idempotency-key",
    default=None,
    help="Signed Candidate key; an exact payload replay returns its durable comment.",
)
@click.option("--json", "as_json", is_flag=True)
def task_comment(
    contract_id: str,
    text: str,
    reply_to: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Append a signed comment to one Contract without changing task state."""
    try:
        validate_comment_text(text)
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
        if not contract_id.strip():
            raise ValueError("contract_id must be a non-empty string")
        if reply_to and not reply_to.strip():
            raise ValueError("reply_to must not be blank")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)

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
        collaboration_service(snapshot).sync()
        validate_comment_target(snapshot, contract_id, reply_to=reply_to)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    finally:
        snapshot.close()

    store = _persistent_store()
    try:
        data, revision = publish_task_comment(
            store,
            contract_id,
            text,
            reply_to=reply_to,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(as_json, data, revision, _human_comment(data))


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


@click.command("block")
@click.argument("contract_ids", nargs=-1, required=True)
@click.option("--reason", required=True, help="Reason for placing the hold.")
@click.option(
    "--idempotency-key",
    default=None,
    help="Signed Candidate replay key or bulk base key.",
)
@click.option("--json", "as_json", is_flag=True)
def task_block(
    contract_ids: tuple[str, ...],
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Place a signed hold on one or more unclaimed, non-terminal tasks."""
    from aigineering.cli._task_batch import (
        collect_task_batch,
        emit_batch,
        new_batch_key_base,
        validate_batch_key,
        validate_task_ids,
    )

    try:
        ids = validate_task_ids(contract_ids)
        validate_comment_text(reason)
        validate_batch_key(idempotency_key)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    if len(ids) == 1:
        contract_id = ids[0]
        require_redis()
        snapshot = _open_snapshot(as_json)
        try:
            try:
                load_genesis(snapshot)
            except LookupError as exc:
                _failure(
                    "workspace_not_initialized",
                    "Initialize this workspace first.",
                    as_json,
                )
                raise AssertionError("unreachable") from exc
            collaboration_service(snapshot).sync()
            validate_task_block(snapshot, contract_id, reason)
        except LookupError as exc:
            _failure("not_found", str(exc), as_json)
        except ValueError as exc:
            _failure("invalid_input", str(exc), as_json)
        finally:
            snapshot.close()
        store = _persistent_store()
        try:
            data, revision = publish_task_block(
                store, contract_id, reason, idempotency_key=idempotency_key
            )
        except RedisSyncPending as exc:
            _pending_failure(exc, as_json)
        except LookupError as exc:
            _failure("not_found", str(exc), as_json)
        except ValueError as exc:
            _failure("rejected", str(exc), as_json)
        finally:
            store.close()
        _success(
            as_json,
            data,
            revision,
            f"Placed hold {data['hold_id']} on {contract_id}: {data['reason']}",
        )
        return
    base = idempotency_key or new_batch_key_base("block")
    _task_preflight(as_json)

    def invoke(contract_id, key):
        store = _persistent_store()
        try:
            return publish_task_block(store, contract_id, reason, idempotency_key=key)
        finally:
            store.close()

    collection = collect_task_batch(
        ids, action="block", batch_key_base=base, invoke=invoke
    )
    emit_batch(collection, as_json=as_json, emit_json=_output_json)


@click.command("unblock")
@click.argument("contract_ids", nargs=-1, required=True)
@click.option(
    "--hold",
    "hold_id",
    default=None,
    help="Exact hold placement record ID (single-target compatibility).",
)
@click.option(
    "--hold-for",
    "hold_for",
    multiple=True,
    help="Exact per-target mapping TASK_ID=HOLD_ID.",
)
@click.option("--reason", required=True, help="Reason for releasing the hold.")
@click.option(
    "--idempotency-key",
    default=None,
    help="Signed Candidate replay key or bulk base key.",
)
@click.option("--json", "as_json", is_flag=True)
def task_unblock(
    contract_ids: tuple[str, ...],
    hold_id: str | None,
    hold_for: tuple[str, ...],
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Release one exact task hold with a signed Candidate."""
    from aigineering.cli._task_batch import (
        collect_task_batch,
        emit_batch,
        exact_target_mapping,
        new_batch_key_base,
        validate_batch_key,
        validate_task_ids,
    )

    try:
        ids = validate_task_ids(contract_ids)
        validate_comment_text(reason)
        validate_batch_key(idempotency_key)
        if hold_id is not None and not hold_id.strip():
            raise ValueError("hold ID must not be blank")
        if hold_id is not None:
            try:
                hold_id.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("hold ID must be valid UTF-8") from exc
        if hold_id is not None and hold_for:
            raise ValueError("use either --hold or --hold-for, not both")
        if len(ids) == 1 and hold_id is None and not hold_for:
            raise click.UsageError("Missing option '--hold'.")
        if len(ids) == 1 and hold_id is not None:
            mapping = {ids[0]: (hold_id,)}
        else:
            if hold_id is not None:
                raise ValueError(
                    "--hold is supported only with one Contract; use --hold-for"
                )
            mapping = exact_target_mapping(hold_for, ids, multiple_per_target=False)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    if len(ids) == 1:
        contract_id = ids[0]
        selected_hold = mapping[contract_id][0]
        require_redis()
        snapshot = _open_snapshot(as_json)
        try:
            try:
                load_genesis(snapshot)
            except LookupError as exc:
                _failure(
                    "workspace_not_initialized",
                    "Initialize this workspace first.",
                    as_json,
                )
                raise AssertionError("unreachable") from exc
            collaboration_service(snapshot).sync()
            validate_task_unblock(snapshot, contract_id, selected_hold, reason)
        except LookupError as exc:
            _failure("not_found", str(exc), as_json)
        except ValueError as exc:
            _failure("invalid_input", str(exc), as_json)
        finally:
            snapshot.close()
        store = _persistent_store()
        try:
            data, revision = publish_task_unblock(
                store,
                contract_id,
                selected_hold,
                reason,
                idempotency_key=idempotency_key,
            )
        except RedisSyncPending as exc:
            _pending_failure(exc, as_json)
        except LookupError as exc:
            _failure("not_found", str(exc), as_json)
        except ValueError as exc:
            _failure("rejected", str(exc), as_json)
        finally:
            store.close()
        _success(
            as_json,
            data,
            revision,
            f"Released hold {selected_hold} with record {data['record_id']}: {data['reason']}",
        )
        return
    base = idempotency_key or new_batch_key_base("unblock")
    _task_preflight(as_json)

    def invoke(contract_id, key):
        store = _persistent_store()
        try:
            return publish_task_unblock(
                store, contract_id, mapping[contract_id][0], reason, idempotency_key=key
            )
        finally:
            store.close()

    collection = collect_task_batch(
        ids, action="unblock", batch_key_base=base, invoke=invoke
    )
    emit_batch(collection, as_json=as_json, emit_json=_output_json)


@click.command("holds")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True)
def task_holds(contract_id: str, as_json: bool) -> None:
    """List all hold placements and their release history for one task."""
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


def _promotion_human(item: dict) -> str:
    lines = [
        f"Promoted Contract {item['contract_id']} with audit {item['record_id']}",
        f"Released holds: {', '.join(item['hold_ids'])}",
        f"Current status: {item['status']}",
    ]
    if item["remaining_hold_ids"]:
        lines.append(f"Remaining holds: {', '.join(item['remaining_hold_ids'])}")
    if item["blockers"]:
        lines.append("Current blockers:")
        lines.extend(f"  {blocker}" for blocker in item["blockers"])
    lines.append(f"Idempotency key: {item['idempotency_key']}")
    return "\n".join(lines)


@click.command("promote")
@click.argument("contract_ids", nargs=-1, required=True)
@click.option(
    "--hold",
    "hold_ids",
    multiple=True,
    help="Exact hold ID(s) for single-target compatibility.",
)
@click.option(
    "--hold-for",
    "hold_for",
    multiple=True,
    help="Exact mapping TASK_ID=HOLD_ID; repeat for a complete target hold set.",
)
@click.option(
    "--reason", required=True, help="Reason for releasing the complete hold set."
)
@click.option(
    "--idempotency-key",
    default=None,
    help="Signed Candidate key/base; save returned key and exact hold IDs for replay.",
)
@click.option("--json", "as_json", is_flag=True)
def task_promote(
    contract_ids: tuple[str, ...],
    hold_ids: tuple[str, ...],
    hold_for: tuple[str, ...],
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Release each selected task's exact complete hold set without resetting claims."""
    from aigineering.cli._task_batch import (
        collect_task_batch,
        emit_batch,
        exact_target_mapping,
        new_batch_key_base,
        validate_batch_key,
        validate_task_ids,
    )

    try:
        ids = validate_task_ids(contract_ids)
        validate_comment_text(reason)
        validate_batch_key(idempotency_key)
        if any(not item.strip() for item in hold_ids):
            raise ValueError("hold IDs must not be blank")
        try:
            for item in hold_ids:
                item.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("hold IDs must be valid UTF-8") from exc
        if len(hold_ids) != len(set(hold_ids)):
            raise ValueError("hold IDs must not contain duplicates")
        if hold_ids and (len(ids) != 1 or hold_for):
            raise ValueError(
                "--hold is single-target only and cannot be combined with --hold-for"
            )
        mapping = (
            exact_target_mapping(hold_for, ids, multiple_per_target=True)
            if hold_for
            else None
        )
        if len(ids) == 1 and hold_ids:
            mapping = {ids[0]: tuple(sorted(hold_ids))}
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)

    if len(ids) == 1:
        contract_id = ids[0]
        _task_preflight(as_json)
        store = _persistent_store()
        try:
            data, revision = publish_task_promote(
                store,
                contract_id,
                reason,
                hold_ids=(mapping[contract_id] if mapping else None),
                idempotency_key=idempotency_key,
            )
        except RedisSyncPending as exc:
            if as_json:
                _output_json(
                    {
                        "ok": False,
                        "error": "redis_sync_pending",
                        "message": str(exc),
                        "facts_retained": True,
                        "candidate_accepted": exc.accepted,
                        "data": exc.data,
                        "revision": exc.revision,
                    }
                )
                raise click.exceptions.Exit(1) from exc
            raise click.ClickException(str(exc)) from exc
        except LookupError as exc:
            _failure("not_found", str(exc), as_json)
        except ValueError as exc:
            _failure("rejected", str(exc), as_json)
        finally:
            store.close()
        _success(as_json, data, revision, _promotion_human(data))
        return

    base = idempotency_key or new_batch_key_base("promote")
    _task_preflight(as_json)

    def invoke(contract_id, key):
        store = _persistent_store()
        try:
            return publish_task_promote(
                store,
                contract_id,
                reason,
                hold_ids=(mapping[contract_id] if mapping else None),
                idempotency_key=key,
            )
        finally:
            store.close()

    collection = collect_task_batch(
        ids,
        action="promote",
        batch_key_base=base,
        invoke=invoke,
        key_for_target=lambda contract_id: f"{base}:promote:{contract_id}",
    )
    emit_batch(collection, as_json=as_json, emit_json=_output_json)


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
        task_comment,
        task_comments,
        task_block,
        task_unblock,
        task_holds,
        task_promote,
        task_promotions,
    ):
        task_group.add_command(copy(command))
