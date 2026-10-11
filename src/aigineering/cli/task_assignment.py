"""Operator commands for signed exact-Contract Worker assignments."""

from __future__ import annotations

from copy import copy
import hashlib
from pathlib import Path
import re
import shlex

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
    exact_target_mapping,
    new_batch_key_base,
    target_key,
    validate_batch_key,
    validate_task_ids,
)
from aigineering.core.domain import load_genesis
from aigineering.core.claim_assignments import AssignmentConflict
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.protocol.identity import canonical_json
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_assignment import (
    AssignmentOperationError,
    publish_task_assignment,
    task_assignment_records,
    validate_task_assignment,
    validate_task_assignment_inputs,
)
from aigineering.task_assignment_requests import (
    AssignmentRequestError,
    load_assignment_request,
    prepare_assignment_request,
    save_assignment_request,
    validate_assignment_request_domain,
    validate_assignment_request_inputs,
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
            "redis_unavailable",
            "Required Redis collaboration is unavailable.",
            as_json,
        )
    except Exception as exc:
        _internal_error(exc, as_json)


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


def _human_assignment(item: dict) -> str:
    worker = item["worker_id"] or "(unassigned)"
    return "\n".join(
        (
            f"Assignment {item['record_id']}",
            f"  Contract: {item['contract_id']}",
            f"  Worker: {worker}",
            f"  previous assignment: {item['previous_assignment_id'] or '(none)'}",
            f"  reason: {item['reason']}",
            f"  actor/key: {item['actor_id']} / {item['key_id']}",
        )
    )


def _reassign_target_key(
    batch_base: str, contract_id: str, previous_assignment_id: str
) -> str:
    payload = canonical_json(
        (
            "task-bulk-v1",
            "reassign",
            batch_base,
            contract_id,
            previous_assignment_id,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8", errors="strict")).hexdigest()
    return f"task-bulk-v1:reassign:{digest}"


def _missing_reassign_parameter(name: str) -> None:
    context = click.get_current_context()
    parameter = next(
        item for item in context.command.params if getattr(item, "name", None) == name
    )
    raise click.MissingParameter(param=parameter, ctx=context)


def _validate_reassign_batch_text(
    worker_id: str | None,
    reason: str,
    idempotency_key: str | None,
    *,
    clear_assignment: bool,
) -> None:
    validate_batch_key(idempotency_key)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be non-empty text")
    try:
        reason_size = len(reason.encode("utf-8", errors="strict"))
        if not clear_assignment:
            worker_size = len(worker_id.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError("assignment text must be valid UTF-8") from exc
    if reason_size > 65536:
        raise ValueError("reason exceeds 65536 UTF-8 bytes")
    if not clear_assignment:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Worker ID must be non-empty text")
        if worker_size > 65536:
            raise ValueError("worker_id exceeds 65536 UTF-8 bytes")


def _task_reassign_batch(
    contract_ids: tuple[str, ...],
    worker_id: str | None,
    clear_assignment: bool,
    previous_for: tuple[str, ...],
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    try:
        targets = validate_task_ids(contract_ids)
        if len(targets) < 2:
            raise ValueError("bulk reassignment requires at least two targets")
        if clear_assignment == (worker_id is not None):
            raise ValueError("choose exactly one of --worker WORKER or --clear")
        _validate_reassign_batch_text(
            worker_id,
            reason,
            idempotency_key,
            clear_assignment=clear_assignment,
        )
        mapped = exact_target_mapping(previous_for, targets, multiple_per_target=False)
        previous_by_target = {target: values[0] for target, values in mapped.items()}
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    batch_base = idempotency_key or new_batch_key_base("reassign")
    target_worker_id = "" if clear_assignment else worker_id

    _require_redis(as_json)
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        _preflight(snapshot, as_json)
    except click.ClickException:
        raise
    except click.exceptions.Exit:
        raise
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        snapshot.close()

    collection = _collect_reassign_targets(
        targets,
        previous_by_target,
        target_worker_id,
        reason,
        batch_base,
    )
    emit_batch(
        collection, as_json=as_json, emit_json=_output_json, safe_human_base=True
    )


def _collect_reassign_targets(
    targets: tuple[str, ...],
    previous_by_target: dict[str, str],
    target_worker_id: str,
    reason: str,
    batch_base: str,
):
    """Run frozen exact-head intents through the existing signed assignment owner."""

    def invoke(contract_id: str, target_idempotency_key: str) -> tuple[dict, int]:
        previous_assignment_id = previous_by_target[contract_id]
        snapshot = _persistent_snapshot()
        try:
            validate_task_assignment(
                snapshot,
                contract_id,
                target_worker_id,
                reason,
                previous_assignment_id=previous_assignment_id,
                require_assigned=True,
                idempotency_key=target_idempotency_key,
            )
        finally:
            snapshot.close()
        store = _persistent_store()
        try:
            return publish_task_assignment(
                store,
                contract_id,
                target_worker_id,
                reason,
                previous_assignment_id=previous_assignment_id,
                require_assigned=True,
                idempotency_key=target_idempotency_key,
            )
        finally:
            store.close()

    collection = collect_task_batch(
        targets,
        action="reassign",
        batch_key_base=batch_base,
        key_for_target=lambda contract_id: _reassign_target_key(
            batch_base, contract_id, previous_by_target[contract_id]
        ),
        invoke=invoke,
        expected_rejection_types=(
            AssignmentOperationError,
            AssignmentConflict,
            CandidateCommitRejected,
        ),
        passthrough_exceptions=(click.ClickException, click.exceptions.Exit),
    )
    for row in collection.data["results"]:
        row["requested_previous_assignment_id"] = previous_by_target[row["contract_id"]]
    return collection


def _request_failure(exc: AssignmentRequestError, as_json: bool) -> None:
    _failure(exc.code, str(exc), as_json)


def _reassign_preflight(as_json: bool, *, request: dict | None = None) -> None:
    _require_redis(as_json)
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        _preflight(snapshot, as_json)
        if request is not None:
            validate_assignment_request_domain(request, snapshot)
    except click.ClickException:
        raise
    except click.exceptions.Exit:
        raise
    except AssignmentRequestError as exc:
        _request_failure(exc, as_json)
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        snapshot.close()


def _request_resume_hint(request_id: str) -> None:
    workspace = shlex.quote(str(Path.cwd().resolve()))
    click.echo(f"Saved reassignment request {request_id}.", err=True)
    click.echo(
        f"Resume with: aig -C {workspace} task reassign --resume {request_id}",
        err=True,
    )


def _current_reassign_request(
    targets: tuple[str, ...],
    worker_id: str | None,
    clear_assignment: bool,
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    try:
        validated_targets = validate_task_ids(targets)
        validate_batch_key(idempotency_key)
        if clear_assignment == (worker_id is not None):
            raise ValueError("choose exactly one of --worker WORKER or --clear")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    batch_base = idempotency_key or new_batch_key_base("reassign")
    try:
        validate_assignment_request_inputs(
            targets=validated_targets,
            worker_id=None if clear_assignment else worker_id,
            reason=reason,
            batch_base=batch_base,
        )
    except AssignmentRequestError as exc:
        _request_failure(exc, as_json)
    _reassign_preflight(as_json)
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        request = prepare_assignment_request(
            snapshot,
            store_path=Path(".aig/store.db"),
            targets=validated_targets,
            worker_id=None if clear_assignment else worker_id,
            reason=reason,
            batch_base=batch_base,
        )
        save_assignment_request(request)
    except AssignmentRequestError as exc:
        _request_failure(exc, as_json)
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        snapshot.close()

    _request_resume_hint(request["request_id"])
    previous_by_target = request["previous_by_target"]
    target_worker_id = "" if request["action"] == "clear" else request["worker_id"]
    collection = _collect_reassign_targets(
        tuple(request["targets"]),
        previous_by_target,
        target_worker_id,
        request["reason"],
        request["batch_base"],
    )
    _emit_reassignment_request(collection, request["request_id"], as_json)


def _resume_reassign_request(request_id: str, as_json: bool) -> None:
    try:
        request = load_assignment_request(Path(".aig/store.db"), request_id)
    except AssignmentRequestError as exc:
        _request_failure(exc, as_json)
    _reassign_preflight(as_json, request=request)
    target_worker_id = "" if request["action"] == "clear" else request["worker_id"]
    collection = _collect_reassign_targets(
        tuple(request["targets"]),
        request["previous_by_target"],
        target_worker_id,
        request["reason"],
        request["batch_base"],
    )
    _emit_reassignment_request(collection, request_id, as_json)


def _emit_reassignment_request(collection, request_id: str, as_json: bool) -> None:
    collection.data["request_id"] = request_id
    for row in collection.data["results"]:
        row["request_id"] = request_id
    emit_batch(
        collection, as_json=as_json, emit_json=_output_json, safe_human_base=True
    )


def _task_assign_batch(
    contract_ids: tuple[str, ...],
    worker_id: str,
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    try:
        targets = validate_task_ids(contract_ids)
        validate_batch_key(idempotency_key)
        validate_task_assignment_inputs(worker_id, reason, idempotency_key)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    batch_key = idempotency_key or new_batch_key_base("assign")

    _require_redis(as_json)
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        _preflight(snapshot, as_json)
    except click.ClickException:
        raise
    except click.exceptions.Exit:
        raise
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        snapshot.close()

    def invoke(contract_id: str, target_idempotency_key: str) -> tuple[dict, int]:
        snapshot = _persistent_snapshot()
        try:
            validate_task_assignment(
                snapshot,
                contract_id,
                worker_id,
                reason,
                previous_assignment_id="",
                require_unassigned=True,
                idempotency_key=target_idempotency_key,
            )
        finally:
            snapshot.close()
        store = _persistent_store()
        try:
            return publish_task_assignment(
                store,
                contract_id,
                worker_id,
                reason,
                previous_assignment_id="",
                require_unassigned=True,
                idempotency_key=target_idempotency_key,
            )
        finally:
            store.close()

    collection = collect_task_batch(
        targets,
        action="assign",
        batch_key_base=batch_key,
        key_for_target=lambda contract_id: target_key("assign", batch_key, contract_id),
        invoke=invoke,
        expected_rejection_types=(
            AssignmentOperationError,
            AssignmentConflict,
            CandidateCommitRejected,
        ),
        passthrough_exceptions=(click.ClickException, click.exceptions.Exit),
    )
    emit_batch(
        collection,
        as_json=as_json,
        emit_json=_output_json,
        safe_human_base=True,
    )


@click.command("assign")
@click.argument("targets", nargs=-1)
@click.argument("worker_id")
@click.option(
    "--reason", required=True, help="Why this Worker is assigned to the Contract."
)
@click.option("--idempotency-key", default=None, help="Signed Candidate replay key.")
@click.option("--json", "as_json", is_flag=True)
def task_assign(
    targets: tuple[str, ...],
    worker_id: str,
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Assign one registered Worker to one or more exact Contracts."""
    if len(targets) != 1:
        _task_assign_batch(targets, worker_id, reason, idempotency_key, as_json)
        return
    target = targets[0]
    try:
        if not target.strip() or not worker_id.strip():
            raise ValueError("target and Worker ID must be non-empty")
        if not reason.strip():
            raise ValueError("reason must be non-empty")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    _require_redis(as_json)
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        _preflight(snapshot, as_json)
        validate_task_assignment(
            snapshot,
            target,
            worker_id,
            reason,
            previous_assignment_id="",
            require_unassigned=True,
            idempotency_key=idempotency_key,
        )
    except click.ClickException:
        raise
    except click.exceptions.Exit:
        raise
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
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
        item, revision = publish_task_assignment(
            store,
            target,
            worker_id,
            reason,
            previous_assignment_id="",
            require_unassigned=True,
            idempotency_key=idempotency_key,
        )
    except RedisSyncPending as exc:
        _pending_failure(exc, as_json)
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        store.close()
    _success(as_json, item, revision, _human_assignment(item))


@click.command("assignments")
@click.argument("target")
@click.option("--json", "as_json", is_flag=True)
def task_assignments(target: str, as_json: bool) -> None:
    """Show exact assignment history and the current Worker binding."""
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
    except click.ClickException:
        raise
    except click.exceptions.Exit:
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
    if not as_json:
        history = data["history"]
        detail = "\n\n".join(_human_assignment(item) for item in history)
        if not detail:
            detail = f"No assignment history for Contract {target}."
        worker = data["worker_id"] or "(unassigned)"
        blocker = data["blocker"]
        suffix = f"\nBlocker: {blocker}" if blocker else ""
        _success(
            False,
            data,
            revision,
            f"Current Worker: {worker}{suffix}\n\n{detail}",
        )
    else:
        _success(True, data, revision, "")


@click.command("reassign")
@click.argument("targets", nargs=-1)
@click.option("--worker", "worker_id", default=None, help="Replacement Worker ID.")
@click.option(
    "--clear",
    "clear_assignment",
    is_flag=True,
    help="Clear the current Worker assignment.",
)
@click.option(
    "--previous",
    "previous_assignment_id",
    default=None,
    help="Exact current assignment head ID, including a clear assignment; the Contract must already have an assignment fact.",
)
@click.option(
    "--previous-for",
    "previous_for",
    multiple=True,
    help="Exact predecessor mapping for a bulk target: CONTRACT=ASSIGNMENT_HEAD.",
)
@click.option(
    "--current",
    "use_current_heads",
    is_flag=True,
    help="Freeze current assignment heads in a recoverable request before publishing.",
)
@click.option(
    "--resume",
    "resume_request_id",
    default=None,
    help="Resume one saved reassignment request by its exact 64-character ID.",
)
@click.option("--reason", default=None, help="Why the Worker binding changes.")
@click.option(
    "--idempotency-key",
    default=None,
    help="Single Candidate replay key or bulk batch base.",
)
@click.option("--json", "as_json", is_flag=True)
def task_reassign(
    targets: tuple[str, ...],
    worker_id: str | None,
    clear_assignment: bool,
    previous_assignment_id: str | None,
    previous_for: tuple[str, ...],
    use_current_heads: bool,
    resume_request_id: str | None,
    reason: str | None,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Replace or clear one or more exact assignment heads."""
    if resume_request_id is not None:
        if (
            targets
            or worker_id is not None
            or clear_assignment
            or previous_assignment_id is not None
            or previous_for
            or use_current_heads
            or reason is not None
            or idempotency_key is not None
        ):
            _failure(
                "invalid_input",
                "--resume cannot be combined with reassignment intent options",
                as_json,
            )
        _resume_reassign_request(resume_request_id, as_json)
        return

    if reason is None:
        _missing_reassign_parameter("reason")
    if use_current_heads:
        if not targets:
            raise click.UsageError("Missing argument 'TARGETS'.")
        if previous_assignment_id is not None or previous_for:
            _failure(
                "invalid_input",
                "--current cannot be combined with --previous or --previous-for",
                as_json,
            )
        _current_reassign_request(
            targets,
            worker_id,
            clear_assignment,
            reason,
            idempotency_key,
            as_json,
        )
        return

    if not targets:
        raise click.UsageError("Missing argument 'TARGET'.")
    if len(targets) >= 2:
        if previous_assignment_id is not None:
            _failure(
                "invalid_input",
                "use --previous-for once for every bulk target; --previous is single-target only",
                as_json,
            )
        _task_reassign_batch(
            targets,
            worker_id,
            clear_assignment,
            previous_for,
            reason,
            idempotency_key,
            as_json,
        )
        return
    target = targets[0]
    if previous_for:
        _failure(
            "invalid_input",
            "--previous-for is only for batches of at least two targets",
            as_json,
        )
    if previous_assignment_id is None:
        _missing_reassign_parameter("previous_assignment_id")
    if clear_assignment == (worker_id is not None):
        _failure(
            "invalid_input",
            "choose exactly one of --worker WORKER or --clear",
            as_json,
        )
    if clear_assignment:
        worker_id = ""
    try:
        if not target.strip() or not reason.strip():
            raise ValueError("target and reason must be non-empty")
        if worker_id and not worker_id.strip():
            raise ValueError("Worker ID must be non-empty")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    _require_redis(as_json)
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except Exception as exc:
        _internal_error(exc, as_json)
    try:
        _preflight(snapshot, as_json)
        validate_task_assignment(
            snapshot,
            target,
            worker_id or "",
            reason,
            previous_assignment_id=previous_assignment_id,
            require_assigned=True,
            idempotency_key=idempotency_key,
        )
    except click.ClickException:
        raise
    except click.exceptions.Exit:
        raise
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
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
        item, revision = publish_task_assignment(
            store,
            target,
            worker_id or "",
            reason,
            previous_assignment_id=previous_assignment_id,
            require_assigned=True,
            idempotency_key=idempotency_key,
        )
    except RedisSyncPending as exc:
        _pending_failure(exc, as_json)
    except QueryProjectionUnavailable:
        _failure(
            "redis_unavailable", "Required Redis collaboration is unavailable.", as_json
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    except Exception as exc:
        _internal_error(exc, as_json)
    finally:
        store.close()
    action = "Cleared" if clear_assignment else "Reassigned"
    _success(as_json, item, revision, f"{action} {target}: {_human_assignment(item)}")


def register_task_assignment_commands(task_group: click.Group) -> None:
    """Register fresh Click command copies so infrastructure wrappers stay local."""
    task_group.add_command(copy(task_assign))
    task_group.add_command(copy(task_reassign))
    task_group.add_command(copy(task_assignments))


__all__ = ["register_task_assignment_commands"]
