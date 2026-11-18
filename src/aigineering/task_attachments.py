"""Read-only historical attachments views."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from aigineering.application import collaboration_service
from aigineering.redis_collaboration import RedisCollaborationUnavailable
from aigineering.business import artifacts
from aigineering.plugins.task_attachments import (
    validate_link_history,
)
from aigineering.core.sqlite_store import SQLiteStore

_PLUGIN_ID = "task.attachments.operator.v1"
_CAPABILITIES = ("task.attach",)
_CURSOR_VERSION = 1


class TaskAttachmentError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def task_attachment_list(
    store, contract_id: str, *, limit: int = 20, cursor: str | None = None
) -> dict:
    _validate_text(contract_id, "contract_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise TaskAttachmentError("invalid_limit")
    if store.get_contract(contract_id) is None:
        raise TaskAttachmentError("task_not_found")
    try:
        collaboration_service(store).sync()
    except RedisCollaborationUnavailable as exc:
        raise TaskAttachmentError("redis_unavailable") from exc
    pairs = store.scan_runtime_records()
    records = tuple(record for _revision, record in pairs)
    _validate_task_materialization(store, records, contract_id)
    revision_by_id = {record.id: revision for revision, record in pairs}
    links = validate_link_history(records, contract_id)
    domain_id = _domain_id(store)
    revision = store.get_runtime_revision()
    watermark = revision
    boundary: tuple[int, str] | None = None
    if cursor is not None:
        token = _decode_cursor(cursor)
        if (
            set(token)
            != {
                "v",
                "domain_id",
                "contract_id",
                "limit",
                "watermark",
                "revision",
                "record_id",
            }
            or type(token.get("v")) is not int
            or token.get("domain_id") != domain_id
            or token.get("contract_id") != contract_id
            or type(token.get("limit")) is not int
            or token.get("limit") != limit
        ):
            raise TaskAttachmentError("cursor_scope_mismatch")
        watermark = token.get("watermark")
        if (
            isinstance(watermark, bool)
            or not isinstance(watermark, int)
            or watermark < 0
            or watermark > revision
        ):
            raise TaskAttachmentError("invalid_cursor")
        cursor_revision = token.get("revision")
        record_id = token.get("record_id")
        if (
            isinstance(cursor_revision, bool)
            or not isinstance(cursor_revision, int)
            or not 0 <= cursor_revision <= watermark
            or not isinstance(record_id, str)
            or not record_id
        ):
            raise TaskAttachmentError("invalid_cursor")
        boundary = (cursor_revision, record_id)
        if boundary not in {
            (revision_by_id[link.id], link.id)
            for link in links
            if revision_by_id[link.id] <= watermark
        }:
            raise TaskAttachmentError("invalid_cursor")
    ordered = sorted(
        (link for link in links if revision_by_id[link.id] <= watermark),
        key=lambda link: (revision_by_id[link.id], link.id),
        reverse=True,
    )
    if boundary is not None:
        ordered = [
            link for link in ordered if (revision_by_id[link.id], link.id) < boundary
        ]
    page = ordered[:limit]
    next_cursor = None
    if len(ordered) > len(page) and page:
        last = page[-1]
        next_cursor = _encode_cursor(
            {
                "v": _CURSOR_VERSION,
                "domain_id": domain_id,
                "contract_id": contract_id,
                "limit": limit,
                "watermark": watermark,
                "revision": revision_by_id[last.id],
                "record_id": last.id,
            }
        )
    metadata = []
    by_id = {record.id: record for record in records}
    for link in page:
        asset_fact = by_id[link.payload["asset_record_id"]]
        raw = asset_fact.payload["asset"]
        envelope = json.loads(raw["content"])
        payload = envelope["payload"]
        metadata.append(
            {
                "record_id": link.id,
                "revision": revision_by_id[link.id],
                "recorded_at": link.recorded_at,
                "contract_id": contract_id,
                "asset_id": link.payload["asset_id"],
                "candidate_id": link.payload["candidate_id"],
                "actor_id": link.payload["actor_id"],
                "key_id": link.payload["key_id"],
                "asset_candidate_id": link.payload["asset_candidate_id"],
                "name": raw["name"],
                "media_type": payload["media_type"],
                "size": payload["size"],
                "sha256": payload["sha256"],
            }
        )
    return {
        "ok": True,
        "data": {
            "contract_id": contract_id,
            "domain_id": domain_id,
            "watermark": watermark,
            "attachments": metadata,
            "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
        },
        "revision": revision,
    }


def export_task_attachment(
    store, contract_id: str, asset_id: str, output: str | Path
) -> dict:
    _validate_text(contract_id, "contract_id")
    _validate_text(asset_id, "asset_id")
    try:
        collaboration_service(store).sync()
    except RedisCollaborationUnavailable as exc:
        raise TaskAttachmentError("redis_unavailable") from exc
    contract = store.get_contract(contract_id)
    if contract is None:
        raise TaskAttachmentError("task_not_found")
    pairs = store.scan_runtime_records()
    records = tuple(record for _revision, record in pairs)
    _validate_task_materialization(store, records, contract_id, contract=contract)
    links = validate_link_history(records, contract_id)
    if not any(link.payload.get("asset_id") == asset_id for link in links):
        raise TaskAttachmentError("attachment_not_linked")
    asset = store.get_asset(asset_id)
    if asset is None:
        raise TaskAttachmentError("asset_not_found")
    asset_fact = next(
        record
        for record in records
        if record.id
        == next(
            link.payload["asset_record_id"]
            for link in links
            if link.payload.get("asset_id") == asset_id
        )
    )
    from aigineering.protocol.immutability import deep_thaw
    from aigineering.protocol.wire import asset_to_dict

    if deep_thaw(asset_fact.payload["asset"]) != deep_thaw(asset_to_dict(asset)):
        raise TaskAttachmentError("attachment_unavailable")
    try:
        data = artifacts.attachment_bytes(asset)
    except (TypeError, ValueError) as exc:
        raise TaskAttachmentError("attachment_unavailable") from exc
    path = Path(output)
    _exclusive_export(path, data)
    return {
        "ok": True,
        "data": {
            "contract_id": contract_id,
            "asset_id": asset_id,
            "output_path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        "revision": store.get_runtime_revision(),
    }


def _validate_task_materialization(store, records, contract_id, *, contract=None):
    declarations = tuple(
        record
        for record in records
        if record.record_type == "contract.declared"
        and record.payload.get("contract", {}).get("id") == contract_id
    )
    if len(declarations) != 1:
        raise TaskAttachmentError("task_history_unavailable")
    try:
        from aigineering.core.contract_admission import validate_contract_commitment
        from aigineering.protocol.immutability import deep_thaw
        from aigineering.protocol.wire import contract_from_dict, contract_to_dict

        declared = contract_from_dict(deep_thaw(declarations[0].payload["contract"]))
        validate_contract_commitment(declared)
        materialized = (
            contract if contract is not None else store.get_contract(contract_id)
        )
        if materialized is None or contract_to_dict(materialized) != contract_to_dict(
            declared
        ):
            raise ValueError("task materialization differs from its declaration")
    except (TypeError, ValueError, KeyError) as exc:
        raise TaskAttachmentError("task_history_unavailable") from exc


def _exclusive_export(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path, flags, 0o600)
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short export write")
            view = view[count:]
        os.fsync(fd)
    except FileExistsError as exc:
        raise TaskAttachmentError("destination_exists") from exc
    except OSError as exc:
        raise TaskAttachmentError("export_failed") from exc
    finally:
        if fd is not None:
            os.close(fd)


def open_task_attachment_store(path: str | Path, *, writable: bool):
    """Resolve one explicit existing Store; reads use a non-writing snapshot."""
    selected = Path(path)
    if not selected.is_file():
        raise TaskAttachmentError("store_unavailable")
    if writable:
        return SQLiteStore(db_path=str(selected))
    return SQLiteStore.snapshot_from_path(selected)


def _unique_record(records, predicate):
    matches = [record for record in records if predicate(record)]
    if len(matches) != 1:
        raise TaskAttachmentError("publication_unavailable")
    return matches[0]


def _domain_id(store):
    from aigineering.core.domain import load_genesis

    return load_genesis(store).id


def _encode_cursor(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()[:16].encode()
    return base64.urlsafe_b64encode(raw + b"." + digest).decode().rstrip("=")


def _decode_cursor(token):
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body, digest = raw.rsplit(b".", 1)
        if hashlib.sha256(body).hexdigest()[:16].encode() != digest:
            raise ValueError
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or type(payload.get("v")) is not int
            or payload.get("v") != _CURSOR_VERSION
        ):
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TaskAttachmentError("invalid_cursor") from exc


def _validate_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise TaskAttachmentError("invalid_arguments")
