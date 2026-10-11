"""Shared JSON and human rendering for both retry CLI entry points."""

from __future__ import annotations

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.cli._common import _output_json
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_publication import RetryPublicationRejected


def emit_retry_success(data: dict, revision: int, *, as_json: bool) -> None:
    """Render the same successful result for ``aig retry`` and ``aig task retry``."""
    payload = {
        "ok": True,
        "data": data,
        "revision": revision,
        # Retain the legacy top-level ID fields while providing the common envelope.
        "original_contract_id": data["original_contract_id"],
        "retry_contract_id": data["retry_contract_id"],
    }
    if as_json:
        _output_json(payload)
        return
    click.echo(f"Retry contract created: {data['retry_contract_id']}")
    click.echo(f"  Budget: {data['budget']}")
    click.echo(f"  Original: {data['original_contract_id']}")


def emit_retry_error(exc: Exception, *, as_json: bool) -> None:
    """Render stable safe retry errors and preserve the originating exception chain."""
    if isinstance(exc, click.ClickException):
        raise exc

    code = "internal_error"
    message = "Task retry failed due to an internal error."
    revision = None
    data = None
    not_found = False
    candidate_accepted = None
    facts_retained = None

    if isinstance(exc, RedisSyncPending):
        code = "redis_sync_pending"
        message = (
            "Retry synchronization is pending; restore Redis and replay the same retry."
        )
        revision = exc.revision
        data = exc.data
        candidate_accepted = exc.accepted
        facts_retained = True
    elif isinstance(exc, QueryProjectionUnavailable):
        code = "redis_unavailable"
        message = "Redis collaboration service is unavailable."
    elif isinstance(exc, RetryPublicationRejected):
        code = "retry_rejected"
        message = "Retry Candidate was rejected; its audit result is retained."
        revision = exc.revision
        data = exc.data
        candidate_accepted = False
        facts_retained = True
    elif isinstance(exc, LookupError):
        code = "not_found"
        message = "Contract was not found."
        not_found = True

    payload = {
        "ok": False,
        "error": {"code": code, "message": message},
        "revision": revision,
    }
    if not_found:
        payload["not_found"] = True
    if candidate_accepted is not None or code == "redis_sync_pending":
        payload["candidate_accepted"] = candidate_accepted
        payload["facts_retained"] = facts_retained
    if isinstance(data, dict):
        payload["data"] = data

    if as_json:
        _output_json(payload)
        raise click.exceptions.Exit(1) from exc

    if (
        code == "redis_sync_pending"
        and candidate_accepted is True
        and isinstance(data, dict)
    ):
        click.echo(
            "Retry committed but Redis synchronization is pending: "
            f"{data.get('retry_contract_id', 'retry result unavailable')}.",
            err=True,
        )
    if code == "retry_rejected" and isinstance(data, dict):
        message += f" Rejection fact: {data.get('rejection_fact_id', 'unavailable')}"
    raise click.ClickException(message) from exc
