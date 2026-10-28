"""Signed application services for task claim dependencies."""

from __future__ import annotations

import uuid
from dataclasses import replace

from aigineering.application import collaboration_service, require_redis
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.claim_constraints import claim_constraint_history
from aigineering.local_identity import ensure_local_plugin_publisher
from aigineering.plugins.task_dependencies import (
    TASK_DEPENDENCY_EFFECTS,
)
from aigineering.protocol.effect_builders import (
    task_dependency_link_effect,
    task_dependency_unlink_effect,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.runtime_services import sync_runtime

_PLUGIN_ID = "task.dependencies.operator.v1"
_CAPABILITIES = ("task.dependency.manage",)
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


def validate_task_dependency_link(
    store,
    contract_id: str,
    prerequisite_contract_id: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
) -> None:
    """Validate immutable command inputs before creating an operator identity."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be non-empty text")
    if (
        not isinstance(prerequisite_contract_id, str)
        or not prerequisite_contract_id.strip()
    ):
        raise ValueError("prerequisite_contract_id must be non-empty text")
    if contract_id == prerequisite_contract_id:
        raise ValueError("a task cannot depend on itself")
    _validate_text(reason, "reason")
    _validate_key(idempotency_key)
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown target Contract {contract_id!r}")
    if store.get_contract(prerequisite_contract_id) is None:
        raise LookupError(f"unknown prerequisite Contract {prerequisite_contract_id!r}")


def validate_task_dependency_unlink(
    store,
    contract_id: str,
    constraint_id: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
) -> None:
    """Validate an exact placement reference before provisioning the actor."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be non-empty text")
    if not isinstance(constraint_id, str) or not constraint_id.strip():
        raise ValueError("constraint_id must be non-empty text")
    _validate_text(reason, "reason")
    _validate_key(idempotency_key)
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown target Contract {contract_id!r}")
    placement = store.get_runtime_record(constraint_id)
    if (
        placement is None
        or placement.record_type != "claim.constraint.placed"
        or placement.payload.get("contract_id") != contract_id
    ):
        raise LookupError(
            "constraint ID must identify a dependency placement on this target"
        )


def _publisher(store):
    return replace(
        ensure_local_plugin_publisher(store, _PLUGIN_ID, _CAPABILITIES),
        additional_effects=TASK_DEPENDENCY_EFFECTS,
    )


def _view(store, proposed, revision: int | None = None) -> dict:
    if proposed is None:
        raise RuntimeError("accepted dependency Candidate produced no constraint fact")
    durable = store.get_runtime_record(proposed.id)
    if durable is None:
        raise RuntimeError("accepted dependency fact is missing from the durable Store")
    if revision is None:
        revision = next(
            (
                item_revision
                for item_revision, record in store.scan_runtime_records(
                    record_type=durable.record_type
                )
                if record.id == durable.id
            ),
            None,
        )
    if revision is None:
        raise RuntimeError("durable dependency fact has no Store revision")
    data = {
        "record_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **deep_thaw(durable.payload),
    }
    if durable.record_type == "claim.constraint.placed":
        data["constraint_id"] = durable.id
    return data


def _publish(store, effect, *, record_type: str, key: str, parents: tuple[str, ...]):
    publisher = _publisher(store)
    # Actor provisioning is itself a durable Candidate and must reach Redis first.
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
        message = (
            str(rejection.payload.get("reason", "Candidate was rejected"))
            if rejection is not None
            else "Candidate was not accepted"
        )
        raise ValueError(message)
    sync_runtime(store, accepted=True)
    proposed = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type == record_type
        ),
        None,
    )
    view = _view(store, proposed)
    return view, store.get_runtime_revision()


def publish_task_dependency_link(
    store,
    contract_id: str,
    prerequisite_contract_id: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish one signed exact prerequisite link for future task claims."""
    require_redis()
    validate_task_dependency_link(
        store,
        contract_id,
        prerequisite_contract_id,
        reason,
        idempotency_key=idempotency_key,
    )
    return _publish(
        store,
        task_dependency_link_effect(contract_id, prerequisite_contract_id, reason),
        record_type="claim.constraint.placed",
        key=idempotency_key or f"task-dependency-link:{uuid.uuid4().hex}",
        parents=(contract_id, prerequisite_contract_id),
    )


def publish_task_dependency_unlink(
    store,
    contract_id: str,
    constraint_id: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Release one exact signed dependency placement."""
    require_redis()
    validate_task_dependency_unlink(
        store,
        contract_id,
        constraint_id,
        reason,
        idempotency_key=idempotency_key,
    )
    return _publish(
        store,
        task_dependency_unlink_effect(contract_id, constraint_id, reason),
        record_type="claim.constraint.released",
        key=idempotency_key or f"task-dependency-unlink:{uuid.uuid4().hex}",
        parents=(contract_id, constraint_id),
    )


def task_dependency_records(store, *, contract_id: str) -> tuple[list[dict], int]:
    """Return immutable link and release history for a target Contract."""
    require_redis()
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown target Contract {contract_id!r}")
    collaboration_service(store).sync()
    pairs = store.scan_runtime_records()
    records = tuple(record for _revision, record in pairs)
    revisions = {record.id: revision for revision, record in pairs}
    by_id = {record.id: record for record in records}
    data = []
    for history in claim_constraint_history(records, contract_id=contract_id):
        item = dict(history)
        placement = by_id[item["constraint_id"]]
        item.update(
            {
                "record_id": placement.id,
                "record_type": placement.record_type,
                "revision": revisions[placement.id],
                "recorded_at": placement.recorded_at,
                "release_refs": [],
            }
        )
        release_id = str(item.pop("released_record_id", ""))
        if release_id:
            released = by_id[release_id]
            item["release_refs"] = [
                {
                    "record_id": released.id,
                    "record_type": released.record_type,
                    "revision": revisions[released.id],
                    "recorded_at": released.recorded_at,
                    "reason": item["release_reason"],
                    "actor_id": item["release_actor_id"],
                    "key_id": item["release_key_id"],
                    "candidate_id": item["release_candidate_id"],
                }
            ]
        data.append(item)
    return data, store.get_runtime_revision()
