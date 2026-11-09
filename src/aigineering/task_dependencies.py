"""Read-only historical dependencies views."""

from __future__ import annotations


from aigineering.application import collaboration_service, require_redis
from aigineering.core.claim_constraints import claim_constraint_history
from aigineering.protocol.immutability import deep_thaw

_PLUGIN_ID = "task.dependencies.operator.v1"
_CAPABILITIES = ("task.dependency.manage",)
_MAX_TEXT_BYTES = 64 * 1024


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
