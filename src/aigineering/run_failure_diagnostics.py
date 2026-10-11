"""Safe application enrichment for a durably closed Worker invocation failure."""

from __future__ import annotations

import json
from typing import Any

from aigineering.task_diagnostics import task_invocation_failure_binding


def invocation_failure_detail(store, claimed) -> dict[str, Any]:
    """Prove one failure from the exact claimed package, or return no IDs."""
    package = claimed.package
    try:
        return task_invocation_failure_binding(
            store,
            claimed.contract.id,
            package.claim_id,
            package.package_id,
            claimed.worker_id,
            package.claim_epoch,
        )
    except Exception:
        # Reporting is read-only enrichment and cannot change the completed
        # invocation. Never turn an unproven selector into a displayed identity.
        return {
            "kind": "invocation_failure",
            "status": "diagnostic_unavailable",
            "category": "diagnostic_unavailable",
        }


def format_worker_failure_detail(detail: dict[str, Any]) -> str:
    """Render fixed safe text and only identifiers admitted by the proof owner."""
    if detail.get("status") == "invocation_failed":
        category = detail.get("category")
        status_code = detail.get("status_code")
        task_id = json.dumps(detail.get("contract_id"), ensure_ascii=True)
        claim_id = json.dumps(detail.get("claim_id"), ensure_ascii=True)
        return (
            f"worker invocation failed for task {task_id} claim {claim_id}; "
            "claim was released; inspect task runs/diagnostics "
            f"(category={category}, status_code={status_code})"
        )
    if detail.get("kind") == "slot_error":
        return "worker slot failed unexpectedly; no claim binding is available"
    return "worker invocation failed; exact claim diagnostics are unavailable"


def safe_worker_error_messages(
    worker_errors: object, worker_failure_details: object
) -> list[str]:
    """Render failures from exact proven details; never echo adapter exception text."""
    errors = list(worker_errors) if isinstance(worker_errors, (list, tuple)) else []
    details = (
        list(worker_failure_details)
        if isinstance(worker_failure_details, (list, tuple))
        else []
    )
    if not errors:
        return []
    if details:
        return [
            format_worker_failure_detail(detail)
            if isinstance(detail, dict)
            else "worker invocation failed; exact claim diagnostics are unavailable"
            for detail in details
        ]
    if errors:
        return [
            "worker invocation failed; exact claim diagnostics are unavailable"
            for _ in errors
        ]
    return []


def safe_task_rejections(
    rejections: object, worker_failure_details: object = ()
) -> list[str]:
    """Omit arbitrary persisted recovery text from human-facing cycle output."""
    del worker_failure_details  # rendering policy does not infer from task facts
    values = list(rejections) if isinstance(rejections, (list, tuple)) else []
    return [
        "[recovery] details omitted"
        if isinstance(value, str) and value.startswith("[recovery] ")
        else value
        for value in values
        if isinstance(value, str)
    ]
