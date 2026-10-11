"""Safe presentation for one configured application cycle."""

from __future__ import annotations

from aigineering.run_failure_diagnostics import (
    safe_task_rejections,
    safe_worker_error_messages,
)


def configured_cycle_payload(result, *, resume_command: str) -> dict:
    """Render the exact cycle projection and state-appropriate next action."""
    payload = {
        "contract_id": result.contract_id,
        "status": result.status,
        "completed": result.completed,
        "timed_out": result.timed_out,
        "worker_errors": safe_worker_error_messages(
            result.worker_errors, result.worker_failure_details
        ),
        "worker_failure_details": list(result.worker_failure_details),
        "capture_warnings": list(result.capture_warnings),
        "stop_reason": result.stop_reason,
        "cycles": list(result.cycles),
        "submission_status": result.submission_status,
        "projection": result.projection,
    }
    projection = dict(result.projection or {})
    projection["rejections"] = safe_task_rejections(
        projection.get("rejections", ()), result.worker_failure_details
    )
    payload["projection"] = projection
    blockers = list(projection.get("blockers", ()))
    payload["blockers"] = blockers
    if result.completed:
        return payload

    payload["reason"] = result.stop_reason
    if "qualification:independent_acceptance" in blockers:
        payload["next_step"] = (
            "Inspect or request the exact independent acceptance report; this task is submitted and should not be rerun."
        )
    elif result.status in {"failed", "cancelled", "unreachable", "stalled"}:
        payload["next_step"] = (
            "Inspect task audit and diagnostics; publish a signed recovery if needed. Claims are never reset."
        )
    elif result.contract_id:
        payload["next_step"] = (
            "Inspect blockers, then resume this exact task for another bounded cycle. A cycle boundary is not completion."
        )
        payload["resume_command"] = resume_command
    else:
        payload["next_step"] = (
            "Inspect the task pool and runtime blockers before another cycle."
        )
    return payload
