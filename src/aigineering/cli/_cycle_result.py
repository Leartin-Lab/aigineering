"""Safe presentation for configured runs and bounded application cycles."""

from __future__ import annotations

from aigineering.run_failure_diagnostics import (
    safe_task_rejections,
    safe_worker_error_messages,
)


def configured_cycle_payload(result, *, resume_command: str) -> dict:
    """Keep the run outcome distinct from the final fact-derived task view."""
    stop_reason = getattr(
        result,
        "stop_reason",
        "deadline"
        if result.timed_out
        else "terminal"
        if result.completed
        else "stopped",
    )
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
        "stop_reason": stop_reason,
        "cycles": list(getattr(result, "cycles", ())),
        "submission_status": getattr(result, "submission_status", None),
    }
    projection = dict(getattr(result, "projection", None) or {})
    projection["rejections"] = safe_task_rejections(
        projection.get("rejections", ()), result.worker_failure_details
    )
    payload["projection"] = projection
    blockers = list(projection.get("blockers", ()))
    payload["blockers"] = blockers
    if result.completed:
        return payload

    payload["reason"] = stop_reason
    task_status = projection.get("status", result.status)
    failed_descendants = [
        blocker.partition(":")[2]
        for blocker in blockers
        if isinstance(blocker, str) and blocker.startswith("descendant_failed:")
    ]
    if task_status == "completed":
        payload["next_step"] = (
            "The task completed while admitted work drained; inspect its outputs "
            f"with aig task result {result.contract_id} --json."
        )
    elif task_status in {"failed", "cancelled", "unreachable", "stalled"}:
        payload["next_step"] = (
            "Inspect task audit and diagnostics; publish new signed work if needed. Claims are never reset."
        )
    elif failed_descendants:
        payload["next_step"] = (
            f"Inspect aig task audit {result.contract_id} --json and failed descendant "
            f"{failed_descendants[0]}; publish fresh verifier or recovery work for "
            "the unmet obligation. Submitted claims are never reset."
        )
    elif "qualification:independent_acceptance" in blockers:
        payload["next_step"] = (
            "Inspect or request the exact independent acceptance report; this task is submitted and should not be rerun."
        )
    elif task_status == "submitted":
        recoveries = list(projection.get("recoveries", ()))
        if recoveries:
            payload["next_step"] = (
                f"Inspect aig task audit {result.contract_id} --json and published "
                f"recovery targets {', '.join(recoveries)}; select only newly "
                "eligible work. The original submitted claim is never reset."
            )
        else:
            payload["next_step"] = (
                f"Inspect aig task audit {result.contract_id} --json; no recovery "
                "target is listed. Publish new signed work for the unmet "
                "obligation. The original submitted claim is never reset."
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
