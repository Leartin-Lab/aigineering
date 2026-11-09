"""Read-only historical collaboration views."""

from __future__ import annotations


from aigineering.application import require_redis
from aigineering.protocol.immutability import deep_thaw
from aigineering.plugins.collaboration import (
    project_task_holds,
    project_task_comments,
)

_COMMENT_PLUGIN_ID = "task.collaboration.operator.v1"
_COMMENT_CAPABILITIES = ("task.comment",)
_HOLDS_PLUGIN_ID = "task.collaboration.holds.operator.v1"
_HOLDS_CAPABILITIES = ("task.block", "task.unblock")


def task_comment_records(store, *, contract_id: str | None = None):
    """Read projected comments with their immutable Store revisions."""
    require_redis()
    from aigineering.application import collaboration_service

    collaboration_service(store).sync()
    pairs = store.scan_runtime_records(record_type="task.commented")
    records = project_task_comments(
        (record for _revision, record in pairs), contract_id=contract_id
    )
    revisions = {record.id: revision for revision, record in pairs}
    current_revision = store.get_runtime_revision()
    data = [
        {
            "record_id": record.id,
            "revision": revisions[record.id],
            "recorded_at": record.recorded_at,
            "contract_id": str(record.payload["contract_id"]),
            "text": str(record.payload["text"]),
            "reply_to": str(record.payload.get("reply_to", "")),
            "actor_id": str(record.payload["actor_id"]),
            "key_id": str(record.payload["key_id"]),
            "candidate_id": str(record.payload["candidate_id"]),
        }
        for record in records
    ]
    return data, current_revision


def _record_view(record, revision: int) -> dict:
    data = {
        "record_id": record.id,
        "record_type": record.record_type,
        "revision": revision,
        "recorded_at": record.recorded_at,
        **deep_thaw(record.payload),
    }
    if record.record_type == "claim.hold.placed":
        data["hold_id"] = record.id
    return data


def task_promotion_records(store, *, contract_id: str) -> tuple[list[dict], int]:
    """Return immutable task.promoted audit history in durable order."""
    require_redis()
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    from aigineering.application import collaboration_service

    collaboration_service(store).sync()
    pairs = store.scan_runtime_records(record_type="task.promoted")
    records = [
        record
        for _revision, record in pairs
        if record.payload.get("contract_id") == contract_id
    ]
    revisions = {record.id: revision for revision, record in pairs}
    return [
        _record_view(record, revisions[record.id]) for record in records
    ], store.get_runtime_revision()


def task_hold_records(store, *, contract_id: str):
    """Return every hold placement, including active state and release record refs."""
    require_redis()
    from aigineering.application import collaboration_service

    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    collaboration_service(store).sync()
    pairs = store.scan_runtime_records()
    records = tuple(record for _revision, record in pairs)
    active_ids = {
        record.id for record in project_task_holds(records, contract_id=contract_id)
    }
    revisions = {record.id: revision for revision, record in pairs}
    releases: dict[str, list] = {}
    for _revision, record in pairs:
        if (
            record.record_type == "claim.hold.released"
            and record.payload.get("contract_id") == contract_id
        ):
            releases.setdefault(str(record.payload.get("hold_id", "")), []).append(
                record
            )
    data = []
    for _record_revision, record in pairs:
        if (
            record.record_type != "claim.hold.placed"
            or record.payload.get("contract_id") != contract_id
        ):
            continue
        linked_releases = releases.get(record.id, [])
        item = _record_view(record, revisions[record.id])
        item["active"] = record.id in active_ids
        item["release_refs"] = [
            {
                "record_id": released.id,
                "revision": revisions[released.id],
                "recorded_at": released.recorded_at,
                "reason": str(released.payload.get("reason", "")),
                "actor_id": str(released.payload.get("actor_id", "")),
                "key_id": str(released.payload.get("key_id", "")),
                "candidate_id": str(released.payload.get("candidate_id", "")),
            }
            for released in linked_releases
        ]
        data.append(item)
    return data, store.get_runtime_revision()
