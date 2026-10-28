"""Signed application services for bounded, review-linked task revisions."""

from __future__ import annotations

import uuid
from dataclasses import replace

from aigineering.application import collaboration_service, require_redis
from aigineering.core.claim_assignments import (
    assignment_context_from_projection,
    assignment_head,
)
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.worker_routing import is_compatible
from aigineering.local_identity import ensure_local_plugin_publisher
from aigineering.plugins.task_revision import (
    TASK_REVISION_EFFECTS,
    project_task_revisions,
    validate_task_revision_references,
)
from aigineering.protocol.effect_builders import task_revision_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.runtime_services import sync_runtime

_PLUGIN_ID = "task.revise.operator.v1"
_CAPABILITIES = ("contract.cancel", "contract.publish", "task.revise")
_MAX_TEXT_BYTES = 64 * 1024


def _validate_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    if size > _MAX_TEXT_BYTES:
        raise ValueError(f"{field} exceeds 65536 UTF-8 bytes")


def _validate_key(idempotency_key: str | None) -> None:
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be non-blank text")


def validate_task_revision(
    store,
    decision_id: str,
    reason: str,
    *,
    budget: int | None = None,
    assignment_worker_id: str | None = None,
    clear_assignment: bool = False,
    source_assignment_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """Validate signed request shape and immutable review references pre-write.

    Source terminal state, allowance, qualification, holds and descendants are
    mutable and intentionally remain the revision projector/writer's decision.
    """
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ValueError("decision ID must be non-empty text")
    _validate_text(reason, "reason")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget < 1
    ):
        raise ValueError("revision budget must be a positive integer when supplied")
    _validate_key(idempotency_key)

    decision = store.get_runtime_record(decision_id)
    if decision is None or decision.record_type != "task.review.decided":
        raise LookupError(f"unknown review decision {decision_id!r}")
    context = load_effect_projection_context(store)
    references = validate_task_revision_references(context, decision_id)
    override_requested = assignment_worker_id is not None or clear_assignment
    if override_requested != (source_assignment_id is not None):
        raise ValueError("assignment override requires an exact source assignment ID")
    if clear_assignment == (assignment_worker_id is not None):
        if override_requested:
            raise ValueError("choose exactly one assignment Worker or clear")
    if assignment_worker_id is not None and not assignment_worker_id.strip():
        raise ValueError("assignment Worker ID must be non-empty")
    if override_requested:
        assignment_context = assignment_context_from_projection(context)
        head = assignment_head(assignment_context, contract_id=references.source.id)
        head_id = head.id if head is not None else ""
        if source_assignment_id != head_id:
            raise ValueError(
                "source assignment ID does not match the exact current head"
            )
        if clear_assignment and (head is None or not head.payload.get("worker_id")):
            raise ValueError(
                "cannot clear an unassigned source; omit the override to inherit"
            )
        if assignment_worker_id is not None:
            registration = next(
                (
                    item
                    for item in context.registrations
                    if item.worker_id == assignment_worker_id
                ),
                None,
            )
            if registration is None or not is_compatible(
                references.source, registration
            ):
                raise ValueError(
                    "assignment override Worker is not currently compatible"
                )


def _publisher(store, *, assignment_override: bool = False):
    plugin_id = (
        "task.revise.assignment-override.operator.v1"
        if assignment_override
        else _PLUGIN_ID
    )
    capabilities = (
        (*_CAPABILITIES, "task.assignment.manage")
        if assignment_override
        else _CAPABILITIES
    )
    return replace(
        ensure_local_plugin_publisher(store, plugin_id, capabilities),
        additional_effects=TASK_REVISION_EFFECTS,
    )


def _revision_view(store, proposed, revision: int | None = None) -> dict:
    if proposed is None:
        raise RuntimeError("accepted revision Candidate produced no task.revised fact")
    durable = store.get_runtime_record(proposed.id)
    if durable is None or durable.record_type != "task.revised":
        raise RuntimeError(
            "accepted task revision link is missing from the durable Store"
        )
    if revision is None:
        revision = next(
            (
                rev
                for rev, record in store.scan_runtime_records(
                    record_type="task.revised"
                )
                if record.id == durable.id
            ),
            None,
        )
    if revision is None:
        raise RuntimeError("durable task revision link has no Store revision")
    return {
        "record_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **deep_thaw(durable.payload),
    }


def _publish(store, publisher, effect, *, key: str, parents: tuple[str, ...]):
    # Ensure any operator authorization is visible before Candidate publication.
    sync_runtime(store)
    try:
        decision = publisher.publish(
            (effect,), idempotency_key=key, causal_parents=parents
        )
    except CandidateCommitRejected:
        sync_runtime(store, accepted=False)
        raise
    if not decision.accepted:
        sync_runtime(store, accepted=False)
        rejection = next(
            (
                record
                for record in decision.runtime_records
                if record.record_type == "candidate.rejected"
            ),
            None,
        )
        reason = (
            str(rejection.payload.get("reason", "Candidate was rejected"))
            if rejection is not None
            else "Candidate was not accepted"
        )
        raise ValueError(reason)
    sync_runtime(store, accepted=True)
    return decision


def publish_task_revision(
    store,
    decision_id: str,
    reason: str,
    *,
    budget: int | None = None,
    assignment_worker_id: str | None = None,
    clear_assignment: bool = False,
    source_assignment_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish a bounded revision Candidate and return its original durable link."""
    require_redis()
    validate_task_revision(
        store,
        decision_id,
        reason,
        budget=budget,
        assignment_worker_id=assignment_worker_id,
        clear_assignment=clear_assignment,
        source_assignment_id=source_assignment_id,
        idempotency_key=idempotency_key,
    )
    override = assignment_worker_id is not None or clear_assignment
    publisher = _publisher(store, assignment_override=override)
    # Plugin actor authorization must be relayed before publishing any decision.
    sync_runtime(store)
    effect = task_revision_effect(
        decision_id,
        reason,
        budget=budget,
        assignment_override=(
            {
                "source_assignment_id": str(source_assignment_id),
                "worker_id": "" if clear_assignment else str(assignment_worker_id),
            }
            if override
            else None
        ),
    )
    decision = _publish(
        store,
        publisher,
        effect,
        key=idempotency_key or f"task-revision:{uuid.uuid4().hex}",
        parents=(
            decision_id,
            *((source_assignment_id,) if override and source_assignment_id else ()),
        ),
    )
    source_id = str(
        store.get_runtime_record(decision_id).payload.get("contract_id", "")
    )
    proposed = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type == "task.revised"
            and record.payload.get("decision_id") == decision_id
            and record.payload.get("contract_id") == source_id
        ),
        None,
    )
    return _revision_view(store, proposed), store.get_runtime_revision()


def task_revision_records(store, source_contract_id: str) -> tuple[list[dict], int]:
    """Read immutable revision links for one source in durable revision order."""
    require_redis()
    if store.get_contract(source_contract_id) is None:
        raise LookupError(f"unknown Contract {source_contract_id!r}")
    collaboration_service(store).sync()
    pairs = store.scan_runtime_records(record_type="task.revised")
    wanted = {
        record.id
        for record in project_task_revisions(
            (record for _revision, record in pairs),
            source_contract_id=source_contract_id,
        )
    }
    data = [
        _revision_view(store, record, revision)
        for revision, record in pairs
        if record.id in wanted
    ]
    return data, store.get_runtime_revision()
