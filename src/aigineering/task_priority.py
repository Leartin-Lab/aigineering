"""Read-only historical priority views."""

from __future__ import annotations


from aigineering.application import collaboration_service, require_redis
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.plugins.task_priority import (
    PRIORITY_FACT,
    priority_history,
)
from aigineering.protocol.immutability import deep_thaw

_PLUGIN_ID = "task.priority.operator.v1"
_CAPABILITIES = ("task.priority.manage",)


def _record_view(store, record):
    durable = store.get_runtime_record(record.id)
    if durable is None or durable.record_type != PRIORITY_FACT:
        raise RuntimeError("priority fact is missing from the durable Store")
    revision = next(
        (
            rev
            for rev, item in store.scan_runtime_records(record_type=PRIORITY_FACT)
            if item.id == durable.id
        ),
        None,
    )
    if revision is None:
        raise RuntimeError("priority fact has no Store revision")
    return {
        "record_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **deep_thaw(durable.payload),
    }


def task_priority_records(store, *, contract_id: str) -> tuple[dict, int]:
    require_redis()
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be non-empty text")
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    collaboration_service(store).sync()
    context = load_effect_projection_context(store)
    has_history = any(
        record.record_type == PRIORITY_FACT
        and record.payload.get("contract_id") == contract_id
        for record in context.runtime_records
    )
    records = priority_history(context, contract_id) if has_history else ()
    history = [_record_view(store, record) for record in records]
    # The revision in this envelope is the snapshot revision, while each row
    # retains its own immutable Store revision.
    return {
        "contract_id": contract_id,
        "priority": int(records[-1].payload["priority"]) if records else 0,
        "head_priority_id": records[-1].id if records else None,
        "history": history,
    }, store.get_runtime_revision()
