"""CLI adapter for safe inspection of the context selected for a task."""

from __future__ import annotations

import json

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import collaboration_service
from aigineering.cli._common import _output_json, _persistent_snapshot
from aigineering.redis_collaboration import RedisCollaborationUnavailable
from aigineering.task_context import TaskContextError, task_context


def register_task_context_command(group: click.Group) -> None:
    @group.command("context")
    @click.argument("contract_id")
    @click.option("--asset", "asset_id", default=None, help="Select an exact Asset ID.")
    @click.option(
        "--include-content", is_flag=True, help="Include selected disclosed content."
    )
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def task_context_command(
        contract_id: str, asset_id: str | None, include_content: bool, as_json: bool
    ) -> None:
        store = None
        try:
            store = _persistent_snapshot()
            collaboration_service(store).sync()
            result = task_context(
                store,
                contract_id,
                asset_id=asset_id,
                include_content=include_content,
            )
        except (RedisCollaborationUnavailable, QueryProjectionUnavailable):
            _emit_error("redis_unavailable", as_json)
            return
        except TaskContextError as error:
            _emit_error(error.code, as_json)
            return
        except Exception:
            _emit_error("inspection_failed", as_json)
            return
        finally:
            if store is not None:
                store.close()
        if as_json:
            _output_json(result)
            return
        data = result["data"]
        click.echo(
            f"Task context for {json.dumps(data['contract_id'], ensure_ascii=True)}"
        )
        for key, title in (
            ("disclosed_assets", "Disclosed assets"),
            ("method_context_assets", "Method context assets"),
        ):
            click.echo(f"{title}:")
            for asset in data[key]:
                click.echo("  " + json.dumps(asset, ensure_ascii=True, sort_keys=True))
        selected = data.get("selected_asset")
        if selected is not None:
            click.echo("Selected content:")
            click.echo(
                selected["content"] if "content" in selected else "(metadata only)"
            )


def _emit_error(code: str, as_json: bool) -> None:
    payload = {"ok": False, "error": code}
    if as_json:
        _output_json(payload)
    else:
        click.echo(f"Task context unavailable ({code})")
    raise click.exceptions.Exit(1)
