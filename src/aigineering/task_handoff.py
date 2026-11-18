"""Read-only historical handoff views."""

from __future__ import annotations

from collections.abc import Mapping

from aigineering.application import collaboration_service, require_redis
from aigineering.plugins.handoff import (
    project_task_handoffs,
)
from aigineering.protocol.immutability import deep_thaw

_PLUGIN_ID = "task.handoff.operator.v1"
_CAPABILITIES = (
    "contract.cancel",
    "contract.publish",
    "task.block",
    "task.handoff",
)
_EDIT_PLUGIN_ID = "task.edit.operator.v1"
_EDIT_CAPABILITIES = (
    "contract.cancel",
    "contract.publish",
    "task.block",
    "task.edit",
    "task.handoff",
)


def _durable_link_view(store, proposed, revision: int | None = None) -> dict:
    if proposed is None:
        raise RuntimeError("accepted task handoff produced no task.handed_off link")
    durable = store.get_runtime_record(proposed.id)
    if durable is None or durable.record_type != "task.handed_off":
        raise RuntimeError(
            "accepted task handoff link is missing from the durable Store"
        )
    # RuntimeRecord payloads are recursively frozen. Thaw the durable wire
    # value before merging it into an application/CLI JSON view so nested
    # mappings and tuples remain JSON objects and arrays.
    payload = deep_thaw(durable.payload)
    if revision is None:
        revision = next(
            (
                item_revision
                for item_revision, item in store.scan_runtime_records(
                    record_type="task.handed_off"
                )
                if item.id == durable.id
            ),
            None,
        )
    if revision is None:
        raise RuntimeError("durable task handoff link has no Store revision")
    successor_id = str(payload["successor_id"])
    hold_id = str(payload.get("hold_id", ""))
    return {
        "record_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **payload,
        "unblock_command": (
            f"aig task unblock {successor_id} --hold {hold_id} --reason 'ready to continue'"
            if hold_id
            else ""
        ),
        "run_command": f"aig run --task {successor_id}",
    }


def task_handoff_records(store, *, contract_id: str) -> tuple[list[dict], int]:
    """Read immutable handoff links and their successor/hold references."""
    require_redis()
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    collaboration_service(store).sync()
    pairs = store.scan_runtime_records(record_type="task.handed_off")
    current_revision = store.get_runtime_revision()
    projected = {
        record.id
        for record in project_task_handoffs(
            (record for _revision, record in pairs), source_contract_id=contract_id
        )
    }
    data = []
    for revision, record in pairs:
        if record.id in projected:
            data.append(_durable_link_view(store, record, revision))
            data[-1]["revision"] = revision
    return data, current_revision


def task_edit_records(store, *, contract_id: str) -> tuple[list[dict], int]:
    """List edit links through the shared immutable handoff history owner."""
    rows, revision = task_handoff_records(store, contract_id=contract_id)
    edits = [item for item in rows if isinstance(item.get("edit"), Mapping)]
    for item in edits:
        item["source_assignment_id"] = str(item["edit"].get("source_assignment_id", ""))
        item["source_hold_ids"] = list(item["edit"].get("source_hold_ids", ()))
    return edits, revision
