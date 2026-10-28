"""CLI adapters for signed task attachment association and exact export."""

from __future__ import annotations

import json
from pathlib import Path

import click

from aigineering.cli._common import _output_json
from aigineering.runtime_services import RedisSyncPending
from aigineering.redis_collaboration import RedisCollaborationUnavailable
from aigineering.task_attachments import (
    TaskAttachmentError,
    add_task_attachment,
    export_task_attachment,
    open_task_attachment_store,
    task_attachment_list,
)


def register_task_attachment_command(group: click.Group) -> None:
    @group.group("attachment")
    def attachment_group() -> None:
        """Associate exact committed attachment Assets with a task."""

    @attachment_group.command("add")
    @click.argument("contract_id")
    @click.argument("asset_id")
    @click.option("--idempotency-key", default=None)
    @click.option(
        "--store",
        "store_path",
        type=click.Path(path_type=Path),
        default=Path(".aig/store.db"),
        show_default=True,
    )
    @click.option("--json", "as_json", is_flag=True)
    def add(
        contract_id: str,
        asset_id: str,
        idempotency_key: str | None,
        store_path: Path,
        as_json: bool,
    ) -> None:
        store = None
        try:
            store = open_task_attachment_store(store_path, writable=True)
            data, revision = add_task_attachment(
                store, contract_id, asset_id, idempotency_key=idempotency_key
            )
        except RedisSyncPending as error:
            payload = {
                "ok": False,
                "error": "redis_sync_pending",
                "candidate_accepted": bool(error.decision and error.decision.accepted),
                "facts_retained": bool(error.decision and error.decision.accepted),
                "data": getattr(error, "data", None),
                "revision": getattr(error, "revision", None),
            }
            _emit(payload, as_json)
            raise click.exceptions.Exit(1)
        except RedisCollaborationUnavailable:
            _emit_error("redis_unavailable", as_json)
        except TaskAttachmentError as error:
            _emit_error(error.code, as_json)
        except Exception:
            _emit_error("attachment_operation_failed", as_json)
        finally:
            if store is not None:
                store.close()
        _emit({"ok": True, "data": data, "revision": revision}, as_json)

    @attachment_group.command("list")
    @click.argument("contract_id")
    @click.option("--limit", type=click.IntRange(1, 100), default=20, show_default=True)
    @click.option("--cursor", default=None)
    @click.option(
        "--store",
        "store_path",
        type=click.Path(path_type=Path),
        default=Path(".aig/store.db"),
        show_default=True,
    )
    @click.option("--json", "as_json", is_flag=True)
    def list_attachments(
        contract_id: str,
        limit: int,
        cursor: str | None,
        store_path: Path,
        as_json: bool,
    ) -> None:
        store = None
        try:
            store = open_task_attachment_store(store_path, writable=False)
            payload = task_attachment_list(
                store, contract_id, limit=limit, cursor=cursor
            )
        except TaskAttachmentError as error:
            _emit_error(error.code, as_json)
        except RedisCollaborationUnavailable:
            _emit_error("redis_unavailable", as_json)
        except Exception:
            _emit_error("attachment_inspection_failed", as_json)
        finally:
            if store is not None:
                store.close()
        _emit(payload, as_json)

    @attachment_group.command("export")
    @click.argument("contract_id")
    @click.argument("asset_id")
    @click.option(
        "--output", "output_path", type=click.Path(path_type=Path), required=True
    )
    @click.option(
        "--store",
        "store_path",
        type=click.Path(path_type=Path),
        default=Path(".aig/store.db"),
        show_default=True,
    )
    @click.option("--json", "as_json", is_flag=True)
    def export(
        contract_id: str,
        asset_id: str,
        output_path: Path,
        store_path: Path,
        as_json: bool,
    ) -> None:
        store = None
        try:
            store = open_task_attachment_store(store_path, writable=False)
            payload = export_task_attachment(store, contract_id, asset_id, output_path)
        except TaskAttachmentError as error:
            _emit_error(error.code, as_json)
        except RedisCollaborationUnavailable:
            _emit_error("redis_unavailable", as_json)
        except Exception:
            _emit_error("attachment_export_failed", as_json)
        finally:
            if store is not None:
                store.close()
        _emit(payload, as_json)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        _output_json(payload)
        return
    click.echo(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _emit_error(code: str, as_json: bool) -> None:
    _emit({"ok": False, "error": code}, as_json)
    raise click.exceptions.Exit(1)
