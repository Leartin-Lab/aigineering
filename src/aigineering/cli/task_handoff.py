"""Operator commands for signed task handoff."""

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
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_handoff import (
    publish_task_handoff,
    publish_task_edit,
    task_edit_records,
    task_handoff_records,
    validate_task_edit,
    validate_task_handoff,
)


def _failure(code: str, message: str, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": {"code": code, "message": message}})
        raise click.exceptions.Exit(1)
    raise click.ClickException(message)


def _redis_pending(exc: RedisSyncPending, as_json: bool) -> None:
    if as_json:
        payload = {
            "ok": False,
            "error": "redis_sync_pending",
            "message": str(exc),
            "facts_retained": exc.accepted is True,
            "candidate_accepted": exc.accepted,
        }
        result = getattr(exc, "result", None)
        if isinstance(result, dict):
            payload["data"] = result
        _output_json(payload)
        raise click.exceptions.Exit(1) from exc
    message = str(exc)
    if exc.accepted is True:
        message += " The edit successor is committed and retained."
    raise click.ClickException(message) from exc


def _success(as_json: bool, data: object, revision: int, human: str) -> None:
    if as_json:
        _output_json({"ok": True, "data": data, "revision": revision})
    else:
        click.echo(human)


def _human_handoff(item: dict) -> str:
    lines = [
        f"Handoff {item['record_id']}",
        f"  source: {item['contract_id']}",
        f"  successor: {item['successor_id']}",
        f"  budget: {item['budget']}",
        f"  reason: {item['reason']}",
    ]
    if item.get("completed_outputs"):
        lines.append(f"  completed outputs: {item['completed_outputs']}")
    if item.get("retained_asset_ids"):
        lines.append(f"  retained Assets: {', '.join(item['retained_asset_ids'])}")
    if "assignment_mode" in item:
        lines.extend(
            (
                f"  successor assignment: {item['assignment_mode']} to "
                f"{item.get('worker_id') or '(unassigned)'}",
                f"  source assignment: {item.get('source_assignment_id') or '(none)'}",
                f"  successor assignment fact: {item.get('assignment_fact_id') or '(none emitted)'}",
            )
        )
    if "source_original_balance" in item:
        lines.extend(
            (
                f"  source original balance: {item['source_original_balance']}",
                f"  returned allowance: {item['returned_allowance']}",
            )
        )
        returns = item.get("return_record_ids", ())
        lines.append("  return records: " + (", ".join(returns) if returns else "none"))
        for returned in item.get("allowance_returns", ()):
            lines.append(
                f"  returned from {returned['donor_contract_id']}: "
                f"{returned['amount']} (fact {returned['record_id']})"
            )
        cancelled = item.get("cancelled_contract_ids", ())
        lines.append(
            "  cancelled descendants: "
            + (", ".join(cancelled) if cancelled else "none")
        )
        cancelled_facts = item.get("cancel_record_ids", ())
        lines.append(
            "  cancellation facts: "
            + (", ".join(cancelled_facts) if cancelled_facts else "none")
        )
        repairs = item.get("repair_contract_ids", ())
        lines.append(
            "  repaired completed descendants: "
            + (", ".join(repairs) if repairs else "none")
        )
        for contract_id, outputs in item.get("repair_output_proofs", {}).items():
            for output in outputs:
                lines.append(
                    f"  repair proof {contract_id}/{output['output_name']}: "
                    f"Assets {', '.join(output['asset_ids'])}; facts "
                    f"{', '.join(output['proof_record_ids']) or 'none'}"
                )
        preserved = item.get("preserved_contract_ids", ())
        lines.append(
            "  preserved verifier branches: "
            + (", ".join(preserved) if preserved else "none")
        )
        for output_name, proof in item.get("retained_output_proofs", {}).items():
            lines.append(
                f"  retained proof {output_name}: Assets "
                f"{', '.join(proof['asset_ids'])}; facts "
                f"{', '.join(proof['proof_record_ids']) or 'none'}"
            )
    if item.get("hold_id"):
        lines.extend(
            (
                f"  successor hold: {item['hold_id']}",
                f"  release with: {item['unblock_command']}",
            )
        )
    if item.get("run_command"):
        lines.append(f"  continue with: {item['run_command']}")
    return "\n".join(lines)


def _human_history(items: list[dict], contract_id: str) -> str:
    if not items:
        return f"No handoffs from Contract {contract_id}."
    return "\n\n".join(_human_handoff(item) for item in items)


@click.command("handoff")
@click.argument("source")
@click.option("--reason", required=True, help="Why the work is being handed off.")
@click.option("--budget", type=click.IntRange(min=1), default=None)
@click.option("--hold-reason", default=None, help="Optionally hold the successor.")
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
def task_handoff(
    source: str,
    reason: str,
    budget: int | None,
    hold_reason: str | None,
    assignment_worker: str | None,
    clear_assignment: bool,
    source_assignment_id: str | None,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Cancel a task tree and publish one bounded successor."""
    if not source.strip():
        _failure("invalid_input", "source Contract ID must be non-empty", as_json)
    try:
        # Structural validation happens before any plugin identity provisioning.
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty text")
        if hold_reason is not None and not hold_reason.strip():
            raise ValueError("hold_reason must be non-empty when supplied")
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
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)

    require_redis()
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable")
    try:
        try:
            load_genesis(snapshot)
        except LookupError:
            _failure(
                "workspace_not_initialized", "Initialize this workspace first.", as_json
            )
        collaboration_service(snapshot).sync()
        validate_task_handoff(
            snapshot,
            source,
            reason,
            budget=budget,
            hold_reason=hold_reason or "",
            assignment_worker_id=assignment_worker,
            clear_assignment=clear_assignment,
            source_assignment_id=source_assignment_id,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except RedisSyncPending as exc:
        _redis_pending(exc, as_json)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    finally:
        snapshot.close()

    store = _persistent_store()
    try:
        data, revision = publish_task_handoff(
            store,
            source,
            reason,
            budget=budget,
            hold_reason=hold_reason or "",
            assignment_worker_id=assignment_worker,
            clear_assignment=clear_assignment,
            source_assignment_id=source_assignment_id,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except RedisSyncPending as exc:
        _redis_pending(exc, as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(as_json, data, revision, _human_handoff(data))


@click.command("handoffs")
@click.argument("source")
@click.option("--json", "as_json", is_flag=True)
def task_handoffs(source: str, as_json: bool) -> None:
    """List immutable handoff links published from one task."""
    if not source.strip():
        _failure("invalid_input", "source Contract ID must be non-empty", as_json)
    require_redis()
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable")
    try:
        try:
            load_genesis(snapshot)
        except LookupError:
            _failure(
                "workspace_not_initialized", "Initialize this workspace first.", as_json
            )
        items, revision = task_handoff_records(snapshot, contract_id=source)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except RedisSyncPending as exc:
        _redis_pending(exc, as_json)
    finally:
        snapshot.close()
    _success(as_json, items, revision, _human_history(items, source))


def register_task_handoff_commands(task_group: click.Group) -> None:
    for command in (task_handoff, task_handoffs, task_edit, task_edits):
        task_group.add_command(copy(command))


def _human_edit(item: dict) -> str:
    lines = [
        f"Task edit {item['record_id']}",
        f"  source: {item['contract_id']} ({item.get('source_status', 'unknown')})",
        f"  successor: {item['successor_id']}",
        f"  changed: {', '.join(item['edit']['changed_fields'])}",
        f"  reason: {item['reason']}",
        f"  assignment: {item.get('successor_assignment_id') or '(unassigned)'}",
    ]
    if item.get("idempotency_key"):
        lines.append(f"  idempotency key: {item['idempotency_key']}")
    mapping = item["edit"].get("hold_mapping", ())
    if mapping:
        lines.append("  copied holds:")
        lines.extend(
            f"    {row['source_hold_id']} -> {row['successor_hold_id']}"
            for row in mapping
        )
    return "\n".join(lines)


def _human_edit_history(items: list[dict], source: str) -> str:
    if not items:
        return f"No edits from Contract {source}."
    return "\n\n".join(
        "\n".join(
            (
                f"Task edit {item['record_id']}",
                f"  successor: {item['successor_id']}",
                f"  changed: {', '.join(item['edit']['changed_fields'])}",
                f"  reason: {item['reason']}",
                f"  source assignment: {item['source_assignment_id'] or '(none)'}",
                "  source holds: " + (", ".join(item["source_hold_ids"]) or "none"),
            )
        )
        for item in items
    )


@click.command("edit")
@click.argument("source")
@click.option("--title", default=None, help="Replace the task title exactly.")
@click.option(
    "--body",
    default=None,
    help="Replace the task body exactly; an empty value is allowed.",
)
@click.option("--reason", required=True, help="Why a successor task is being created.")
@click.option(
    "--source-assignment-id",
    default=None,
    help="Optional exact source assignment head for replay.",
)
@click.option(
    "--source-hold-id",
    "source_hold_ids",
    multiple=True,
    help="Exact source hold ID; repeat each returned ID when replaying an edit.",
)
@click.option("--idempotency-key", default=None, help="Signed Candidate replay key.")
@click.option("--json", "as_json", is_flag=True)
def task_edit(
    source: str,
    title: str | None,
    body: str | None,
    reason: str,
    source_assignment_id: str | None,
    source_hold_ids: tuple[str, ...],
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Edit a task by publishing a bounded successor Contract."""
    if not source.strip():
        _failure("invalid_input", "source Contract ID must be non-empty", as_json)
    try:
        if title is None and body is None:
            raise ValueError("supply --title, --body, or both")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty text")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
        if len(source_hold_ids) != len(set(source_hold_ids)):
            raise ValueError("source hold IDs must not be repeated")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)

    require_redis()
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable")
    try:
        try:
            load_genesis(snapshot)
        except LookupError:
            _failure(
                "workspace_not_initialized", "Initialize this workspace first.", as_json
            )
        collaboration_service(snapshot).sync()
        validate_task_edit(
            snapshot,
            source,
            reason,
            title=title,
            body=body,
            source_assignment_id=source_assignment_id,
            source_hold_ids=source_hold_ids if source_hold_ids else None,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except RedisSyncPending as exc:
        _redis_pending(exc, as_json)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    finally:
        snapshot.close()

    store = _persistent_store()
    try:
        item, revision = publish_task_edit(
            store,
            source,
            reason,
            title=title,
            body=body,
            source_assignment_id=source_assignment_id,
            source_hold_ids=source_hold_ids if source_hold_ids else None,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except RedisSyncPending as exc:
        _redis_pending(exc, as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(as_json, item, revision, _human_edit(item))


@click.command("edits")
@click.argument("source")
@click.option("--json", "as_json", is_flag=True)
def task_edits(source: str, as_json: bool) -> None:
    """List immutable task edit successors from one source Contract."""
    if not source.strip():
        _failure("invalid_input", "source Contract ID must be non-empty", as_json)
    require_redis()
    try:
        snapshot = _persistent_snapshot()
    except FileNotFoundError:
        _failure(
            "workspace_not_initialized", "Initialize this workspace first.", as_json
        )
        raise AssertionError("unreachable")
    try:
        try:
            load_genesis(snapshot)
        except LookupError:
            _failure(
                "workspace_not_initialized", "Initialize this workspace first.", as_json
            )
        items, revision = task_edit_records(snapshot, contract_id=source)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except RedisSyncPending as exc:
        _redis_pending(exc, as_json)
    finally:
        snapshot.close()
    _success(
        as_json,
        items,
        revision,
        _human_edit_history(items, source),
    )
