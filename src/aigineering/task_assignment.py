"""Signed application services for exact-Contract Worker assignments."""

from __future__ import annotations

import uuid
from dataclasses import replace

from aigineering.application import collaboration_service, require_redis
from aigineering.core.claim_assignments import (
    AssignmentMode,
    AssignmentTransition,
    assignment_context_from_projection,
    assignment_history,
    assignment_head,
    effective_assignment,
    validate_assignment_request,
)
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.local_identity import ensure_local_plugin_publisher
from aigineering.local_publication import (
    verify_durable_candidate_member,
    verify_durable_rejection_member,
)
from aigineering.plugins.task_assignment import (
    TASK_ASSIGNMENT_EFFECTS,
)
from aigineering.protocol.effect_builders import task_assign_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.wire import contract_to_dict
from aigineering.runtime_services import RedisSyncPending, sync_runtime

_PLUGIN_ID = "task.assignment.operator.v1"
_CAPABILITIES = ("task.assignment.manage",)
_MAX_TEXT_BYTES = 64 * 1024


class AssignmentOperationError(ValueError):
    """Expected invalid assignment intent or a known current-state refusal."""


def _validate_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AssignmentOperationError(f"{field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise AssignmentOperationError(f"{field} must be valid UTF-8") from exc
    if size > _MAX_TEXT_BYTES:
        raise AssignmentOperationError(f"{field} exceeds 65536 UTF-8 bytes")


def _validate_key(idempotency_key: str | None) -> None:
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise AssignmentOperationError("idempotency_key must be non-blank text")


def validate_task_assignment_inputs(
    worker_id: str, reason: str, idempotency_key: str | None = None
) -> None:
    """Validate assignment text without opening or mutating a Store."""
    _validate_text(worker_id, "worker_id")
    _validate_text(reason, "reason")
    _validate_key(idempotency_key)


def _declaration(store, contract):
    expected = deep_thaw(contract_to_dict(contract))
    matches = tuple(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="contract.declared"
        )
        if record.payload.get("contract", {}).get("id") == contract.id
        and deep_thaw(record.payload.get("contract", {})) == expected
    )
    if len(matches) != 1:
        raise AssignmentOperationError(
            "target Contract has no unique exact declaration fact"
        )
    return matches[0]


def validate_task_assignment(
    store,
    contract_id: str,
    worker_id: str,
    reason: str,
    *,
    previous_assignment_id: str | None = None,
    require_unassigned: bool = False,
    require_assigned: bool = False,
    idempotency_key: str | None = None,
) -> None:
    """Validate static assignment inputs before provisioning an operator key."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise AssignmentOperationError("contract_id must be non-empty text")
    if not isinstance(worker_id, str):
        raise AssignmentOperationError("worker_id must be text")
    _validate_text(reason, "reason")
    _validate_key(idempotency_key)
    contract = store.get_contract(contract_id)
    if contract is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    context = assignment_context_from_projection(load_effect_projection_context(store))
    current_head = assignment_head(context, contract_id=contract_id)
    current_head_id = current_head.id if current_head is not None else ""
    replay_previous_id = _replay_previous_assignment_id(
        context,
        contract_id,
        worker_id,
        reason,
        previous_assignment_id=(
            previous_assignment_id if previous_assignment_id is not None else ""
        ),
    )
    replay_candidate = replay_previous_id is not None
    if require_unassigned and current_head is not None and not replay_candidate:
        raise AssignmentOperationError(
            "Contract already has an assignment; use task reassign"
        )
    if require_assigned and current_head is None:
        raise AssignmentOperationError("Contract has no assignment; use task assign")
    if previous_assignment_id is not None and previous_assignment_id != current_head_id:
        if not replay_candidate:
            raise AssignmentOperationError(
                "previous assignment ID does not match the current head"
            )
    if replay_candidate:
        # The publisher will reconstruct the signed Candidate and the core
        # committer will return the exact committed projection on replay.
        _declaration(store, contract)
        return
    transition = AssignmentTransition(
        target_contract_id=contract_id,
        worker_id=worker_id,
        previous_assignment_id=current_head_id,
        reason=reason,
        mode=AssignmentMode.DIRECT,
    )
    validate_assignment_request(
        context,
        transition,
        target_contract=contract,
        target_declaration_record=_declaration(store, contract),
    )


def _replay_previous_assignment_id(
    context,
    contract_id: str,
    worker_id: str,
    reason: str,
    *,
    previous_assignment_id: str,
) -> str | None:
    from aigineering.core.claim_assignments import assignment_history

    prior = next(
        (
            record
            for record in assignment_history(context, contract_id=contract_id)
            if record.payload.get("actor_id") == "plugin:task.assignment.operator.v1"
            and record.payload.get("contract_id") == contract_id
            and record.payload.get("worker_id") == worker_id
            and record.payload.get("reason") == reason
            and record.payload.get("previous_assignment_id") == previous_assignment_id
        ),
        None,
    )
    return (
        str(prior.payload.get("previous_assignment_id", ""))
        if prior is not None
        else None
    )


def _publisher(store):
    return replace(
        ensure_local_plugin_publisher(store, _PLUGIN_ID, _CAPABILITIES),
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )


def _record_view(store, record, revision: int | None = None) -> dict:
    if record is None:
        raise RuntimeError("accepted assignment Candidate produced no assignment fact")
    durable = store.get_runtime_record(record.id)
    if durable is None or durable.record_type != "claim.assignment.changed":
        raise RuntimeError("assignment fact is missing from the durable Store")
    if revision is None:
        revision = next(
            (
                rev
                for rev, item in store.scan_runtime_records(
                    record_type=durable.record_type
                )
                if item.id == durable.id
            ),
            None,
        )
    if revision is None:
        raise RuntimeError("durable assignment fact has no Store revision")
    return {
        "record_id": durable.id,
        "assignment_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **deep_thaw(durable.payload),
    }


def _accepted_assignment_result(store, decision, contract_id, worker_id, reason, key):
    matches = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "claim.assignment.changed"
        and record.payload.get("candidate_id") == decision.candidate_id
        and record.payload.get("actor_id") == f"plugin:{_PLUGIN_ID}"
        and record.payload.get("contract_id") == contract_id
        and record.payload.get("worker_id") == worker_id
        and record.payload.get("reason") == reason
    )
    if len(matches) != 1:
        raise RuntimeError("accepted assignment Candidate has no unique exact fact")
    durable = verify_durable_candidate_member(store, decision, matches[0])
    revisions = {
        record.id: revision
        for revision, record in store.scan_runtime_records(
            record_type="claim.assignment.changed"
        )
    }
    if durable.id not in revisions:
        raise RuntimeError("durable assignment fact has no Store revision")
    item = _record_view(store, durable, revisions[durable.id])
    if item.get("candidate_id") != decision.candidate_id:
        raise RuntimeError("durable assignment fact has a mismatched Candidate ID")
    item["idempotency_key"] = key
    item["fact_ids"] = [durable.id]
    return item, store.get_runtime_revision()


def _attach_rejection_pending(store, decision, pending: RedisSyncPending) -> None:
    rejection = verify_durable_rejection_member(store, decision)
    pending.accepted = False
    pending.decision = decision
    pending.data = {
        "rejection_fact_id": rejection.id,
        "fact_ids": [rejection.id],
    }
    pending.revision = store.get_runtime_revision()


def publish_task_assignment(
    store,
    contract_id: str,
    worker_id: str,
    reason: str,
    *,
    previous_assignment_id: str | None = None,
    require_unassigned: bool = False,
    require_assigned: bool = False,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish one signed direct assignment, replacement, or clear."""
    require_redis()
    validate_task_assignment(
        store,
        contract_id,
        worker_id,
        reason,
        previous_assignment_id=previous_assignment_id,
        require_unassigned=require_unassigned,
        require_assigned=require_assigned,
        idempotency_key=idempotency_key,
    )
    publisher = _publisher(store)
    sync_runtime(store)
    context = assignment_context_from_projection(load_effect_projection_context(store))
    head = assignment_head(context, contract_id=contract_id)
    previous_id = head.id if head is not None else ""
    signed_previous_id = (
        previous_assignment_id if previous_assignment_id is not None else previous_id
    )
    replay_previous = _replay_previous_assignment_id(
        context,
        contract_id,
        worker_id,
        reason,
        previous_assignment_id=(
            previous_assignment_id if previous_assignment_id is not None else ""
        ),
    )
    if replay_previous is not None:
        prior = next(
            (
                record
                for _revision, record in store.scan_runtime_records(
                    record_type="claim.assignment.changed"
                )
                if record.payload.get("actor_id") == _PLUGIN_ID
                and record.payload.get("contract_id") == contract_id
                and record.payload.get("worker_id") == worker_id
                and record.payload.get("reason") == reason
                and record.payload.get("previous_assignment_id") == replay_previous
            ),
            None,
        )
        if prior is not None:
            signed_previous_id = replay_previous
    key = idempotency_key or f"task-assignment:{uuid.uuid4().hex}"
    try:
        decision = publisher.publish(
            (
                task_assign_effect(
                    contract_id,
                    worker_id,
                    signed_previous_id,
                    reason,
                ),
            ),
            idempotency_key=key,
            causal_parents=(contract_id,),
        )
    except CandidateCommitRejected as exc:
        try:
            sync_runtime(store, accepted=False)
        except RedisSyncPending as pending:
            _attach_rejection_pending(store, exc.decision, pending)
            raise
        raise
    if not decision.accepted:
        try:
            sync_runtime(store, accepted=False)
        except RedisSyncPending as pending:
            _attach_rejection_pending(store, decision, pending)
            raise
        rejection = next(
            (
                record
                for record in decision.runtime_records
                if record.record_type == "candidate.rejected"
            ),
            None,
        )
        message = (
            str(rejection.payload.get("reason", "Candidate was rejected"))
            if rejection is not None
            else "Candidate was not accepted"
        )
        raise AssignmentOperationError(message)
    item, revision = _accepted_assignment_result(
        store, decision, contract_id, worker_id, reason, key
    )
    try:
        sync_runtime(store, accepted=True)
    except RedisSyncPending as pending:
        pending.accepted = True
        pending.decision = decision
        pending.data = item
        pending.revision = revision
        raise
    return item, revision


def task_assignment_records(store, contract_id: str) -> tuple[dict, int]:
    """Return immutable assignment history plus its current exact head."""
    require_redis()
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    collaboration_service(store).sync()
    pairs = store.scan_runtime_records()
    revisions = {record.id: revision for revision, record in pairs}
    context = assignment_context_from_projection(load_effect_projection_context(store))
    history = assignment_history(context, contract_id=contract_id)
    rows = [_record_view(store, record, revisions.get(record.id)) for record in history]
    head = assignment_head(context, contract_id=contract_id)
    effective = effective_assignment(context, contract_id=contract_id)
    from aigineering.core.claim_assignments import assignment_blocker

    blocker = assignment_blocker(context, contract_id=contract_id)
    data = {
        "contract_id": contract_id,
        "head_assignment_id": head.id if head is not None else None,
        "worker_id": str(effective.payload["worker_id"]) if effective else None,
        "blocker": blocker,
        "history": rows,
    }
    return data, store.get_runtime_revision()
