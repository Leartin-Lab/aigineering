"""Operator commands for signed task handoff."""

from __future__ import annotations

import click

from aigineering.application import require_redis
from aigineering.cli._common import (
    _cli_result_success as _success,
    _output_json,
    _persistent_snapshot,
)
from aigineering.core.domain import load_genesis
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_handoff import (
    task_edit_records,
    task_handoff_records,
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
        message += " Previously committed facts are retained."
    raise click.ClickException(message) from exc


def _human_handoff(item: dict) -> str:
    lines = [
        f"Historical handoff {item['record_id']}",
        f"  source: {item['contract_id']}",
        f"  successor: {item['successor_id']}",
        f"  budget: {item['budget']}",
        f"  reason: {item['reason']}",
    ]
    if item.get("completed_outputs"):
        lines.append(f"  completed outputs: {item['completed_outputs']}")
    if item.get("retained_asset_ids"):
        lines.append(f"  retained Assets: {', '.join(item['retained_asset_ids'])}")
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
    return "\n".join(lines)


def _human_history(items: list[dict], contract_id: str) -> str:
    if not items:
        return f"No handoffs from Contract {contract_id}."
    return "\n\n".join(_human_handoff(item) for item in items)


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
    task_group.add_command(task_handoffs)
    task_group.add_command(task_edits)


def _human_edit_history(items: list[dict], source: str) -> str:
    if not items:
        return f"No edits from Contract {source}."
    return "\n\n".join(
        "\n".join(
            (
                f"Historical task edit {item['record_id']}",
                f"  successor: {item['successor_id']}",
                f"  changed: {', '.join(item['edit']['changed_fields'])}",
                f"  reason: {item['reason']}",
                f"  recorded source assignment: {item['source_assignment_id'] or '(none)'}",
                "  source holds: " + (", ".join(item["source_hold_ids"]) or "none"),
            )
        )
        for item in items
    )


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
