"""Read-only historical assignment views."""

from __future__ import annotations


from aigineering.application import collaboration_service, require_redis
from aigineering.core.claim_assignments import (
    assignment_context_from_projection,
    assignment_history,
    assignment_head,
    effective_assignment,
)
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.protocol.immutability import deep_thaw

_PLUGIN_ID = "task.assignment.operator.v1"
_CAPABILITIES = ("task.assignment.manage",)
_MAX_TEXT_BYTES = 64 * 1024


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
    data = {
        "contract_id": contract_id,
        "historical_head_assignment_id": head.id if head is not None else None,
        "historical_worker_id": str(effective.payload["worker_id"])
        if effective
        else None,
        "history": rows,
    }
    return data, store.get_runtime_revision()
