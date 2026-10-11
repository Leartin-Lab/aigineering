"""Commands for requesting and recording policy-bound task reviews."""

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
from aigineering.task_policy_review import (
    publish_task_review_decision,
    publish_task_review_request,
    task_review_records,
    validate_task_review_decision,
    validate_task_review_request,
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


def _open_snapshot(as_json: bool):
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


def _human_request(item: dict) -> str:
    child = item["child_contract_id"]
    worker_id = f"reviewer:{child[-12:]}"
    return "\n".join(
        (
            f"Review request {item['record_id']}",
            f"  source: {item['contract_id']}",
            f"  target: {item['output_name']} → {item['asset_id']}",
            f"  reviewer task: {child} (budget {item['budget']} from source allowance)",
            "  Reserved review budget reduces allowance available to later source work.",
            f"  reason: {item['reason']}",
            f"  run with: aig run --task {child} --worker-id {worker_id}",
            "  The reviewer task creates a report; its completion does not accept the source.",
        )
    )


def _human_decision(item: dict) -> str:
    lines = [
        f"Review decision {item['record_id']}",
        f"  source: {item['contract_id']}",
        f"  reviewer task: {item['child_contract_id']}",
        f"  report: {item['report_asset_id']}",
        f"  verdict: {item['verdict']} (policy-bound, not a universal truth claim)",
        f"  reason: {item['reason']}",
    ]
    if item["verdict"] == "changes":
        lines.append(
            "  source remains unqualified; this command does not create a revision task."
        )
        lines.append("  Any revision needs separately available source allowance.")
    return "\n".join(lines)


def _human_history(items: list[dict], source: str) -> str:
    if not items:
        return f"No review requests or decisions for Contract {source}."
    rendered = []
    for item in items:
        if item["record_type"] == "task.review.requested":
            rendered.append(_human_request(item))
        else:
            rendered.append(_human_decision(item))
    return "\n\n".join(rendered)


@click.command("request-review")
@click.argument("source")
@click.option(
    "--output", "output_name", required=True, help="Exact declared source output slot."
)
@click.option(
    "--asset", "asset_id", required=True, help="Exact committed output Asset ID."
)
@click.option(
    "--reason", required=True, help="Why this output needs independent review."
)
@click.option(
    "--budget",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Reviewer allowance reserved from the source Contract.",
)
@click.option("--idempotency-key", default=None, help="Signed Candidate replay key.")
@click.option("--json", "as_json", is_flag=True)
def task_request_review(
    source: str,
    output_name: str,
    asset_id: str,
    reason: str,
    budget: int,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Create a bounded ordinary task to review one exact source output."""
    try:
        if not source.strip():
            raise ValueError("source Contract ID must be non-empty")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)

    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        _preflight(snapshot, as_json)
        validate_task_review_request(
            snapshot,
            source,
            output_name,
            asset_id,
            reason,
            budget=budget,
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
        data, revision = publish_task_review_request(
            store,
            source,
            output_name,
            asset_id,
            reason,
            budget=budget,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(as_json, data, revision, _human_request(data))


@click.command("reviews")
@click.argument("source")
@click.option("--json", "as_json", is_flag=True)
def task_reviews(source: str, as_json: bool) -> None:
    """List immutable review request and decision facts for a source task."""
    if not source.strip():
        _failure("invalid_input", "source Contract ID must be non-empty", as_json)
    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        _preflight(snapshot, as_json)
        data, revision = task_review_records(snapshot, source)
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    finally:
        snapshot.close()
    _success(as_json, data, revision, _human_history(data, source))


@click.command("decide")
@click.argument("request_id")
@click.option(
    "--report",
    "report_asset_id",
    required=True,
    help="Exact report Asset from the reviewer task.",
)
@click.option(
    "--verdict",
    type=click.Choice(("accept", "changes")),
    required=True,
    help="Policy-bound review decision; accept may qualify the exact source output.",
)
@click.option("--reason", required=True, help="Decision rationale.")
@click.option("--idempotency-key", default=None, help="Signed Candidate replay key.")
@click.option(
    "--actor-id",
    default=None,
    help="Existing authorized domain actor (custom identity tuple).",
)
@click.option("--key-id", default=None, help="Existing authorized actor key ID.")
@click.option(
    "--key-file",
    type=click.Path(path_type=str, exists=True, dir_okay=False),
    default=None,
    help="Matching private Ed25519 key file; permissions must be 0600 on POSIX.",
)
@click.option("--json", "as_json", is_flag=True)
def task_review_decide(
    request_id: str,
    report_asset_id: str,
    verdict: str,
    reason: str,
    idempotency_key: str | None,
    actor_id: str | None,
    key_id: str | None,
    key_file: str | None,
    as_json: bool,
) -> None:
    """Record a policy-bound decision for an exact reviewer report Asset."""
    try:
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
        supplied = (actor_id is not None, key_id is not None, key_file is not None)
        if any(supplied) and not all(supplied):
            raise ValueError(
                "custom decision identity requires --actor-id, --key-id, and --key-file together"
            )
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)

    require_redis()
    snapshot = _open_snapshot(as_json)
    try:
        _preflight(snapshot, as_json)
        validate_task_review_decision(
            snapshot,
            request_id,
            report_asset_id,
            verdict,
            reason,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            key_id=key_id,
            key_file=key_file,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("invalid_input", str(exc), as_json)
    finally:
        snapshot.close()

    store = _persistent_store()
    try:
        data, revision = publish_task_review_decision(
            store,
            request_id,
            report_asset_id,
            verdict,
            reason,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            key_id=key_id,
            key_file=key_file,
        )
    except LookupError as exc:
        _failure("not_found", str(exc), as_json)
    except ValueError as exc:
        _failure("rejected", str(exc), as_json)
    finally:
        store.close()
    _success(as_json, data, revision, _human_decision(data))


def register_task_policy_review_commands(task_group: click.Group) -> None:
    task_group.add_command(copy(task_request_review))
    task_group.add_command(copy(task_reviews))
    review_group = click.Group(
        "review", help="Record a policy-bound decision from an exact review report."
    )
    review_group.add_command(copy(task_review_decide))
    task_group.add_command(review_group)
