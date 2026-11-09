"""Read-only historical revision views."""

from __future__ import annotations


from aigineering.application import collaboration_service, require_redis
from aigineering.plugins.task_revision import (
    project_task_revisions,
)
from aigineering.protocol.immutability import deep_thaw

_PLUGIN_ID = "task.revise.operator.v1"
_CAPABILITIES = ("contract.cancel", "contract.publish", "task.revise")
_MAX_TEXT_BYTES = 64 * 1024


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
