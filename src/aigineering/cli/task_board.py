"""Redis-indexed, fact-projected task board commands."""

from __future__ import annotations

from pathlib import Path

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import (
    collaboration_service,
    persistent_store,
    query_projection,
    require_redis,
)
from aigineering.cli._common import _output_json, _persistent_snapshot
from aigineering.cli._retry_result import emit_retry_error, emit_retry_success
from aigineering.cli._task_batch import (
    collect_task_batch,
    emit_batch,
    new_batch_key_base,
    preflight_task_batch,
    validate_task_ids,
)
from aigineering.core.output_satisfaction import (
    all_outputs_satisfied,
    is_business_output,
)
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_publication import (
    RetryPublicationRejected,
    retry_local_task_result,
)
from aigineering.task_views import (
    contract_tree_ids,
    project_task_status,
    task_priority_histories,
)
from aigineering.task_view_query import (
    TaskViewQueryMismatch,
    load_task_view_query_context,
)
from aigineering.task_priority import order_task_contract_ids


def register_task_board_commands(task_group: click.Group) -> None:
    """Register unified board and task lifecycle commands on ``task``."""
    for command in (
        task_list,
        task_board,
        task_stats,
        task_show,
        task_result,
        task_cancel,
        task_retry,
    ):
        task_group.add_command(command)


def _snapshot_context():
    """Open one read snapshot, synchronize Redis events, and read its index."""
    store = _persistent_snapshot()
    try:
        collaboration_service(store).sync()
        projection = query_projection(store)
        contracts = projection.get_all_contracts()
        revision = store.get_runtime_revision()
        return store, projection, contracts, revision
    except Exception:
        store.close()
        raise


def _close_context(context) -> None:
    if context is not None:
        context[0].close()


def _emit_success(data, revision: int, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": True, "data": data, "revision": revision})


def _emit_failure(
    message: str,
    revision: int | None,
    as_json: bool,
    *,
    code: str = "task_error",
    exit_code: int = 1,
) -> None:
    if as_json:
        _output_json(
            {
                "ok": False,
                "error": {"code": code, "message": message},
                "revision": revision,
            }
        )
        raise click.exceptions.Exit(exit_code)
    if exit_code == 2:
        raise click.UsageError(message)
    raise click.ClickException(message)


def _safe_error(prefix: str, exc: BaseException) -> str:
    """Report failure class without interpolating store/Redis exception text."""
    return f"{prefix} ({type(exc).__name__})"


def _emit_internal_error(
    message: str, exc: BaseException, revision, as_json: bool
) -> None:
    if as_json:
        _output_json(
            {
                "ok": False,
                "error": {"code": "internal_error", "message": message},
                "revision": revision,
            }
        )
        raise click.exceptions.Exit(1) from exc
    raise click.ClickException(message) from exc


def _root_id(contract, by_id: dict[str, object]) -> str:
    current = contract
    seen: set[str] = set()
    while current.parent_id and current.parent_id in by_id and current.id not in seen:
        seen.add(current.id)
        current = by_id[current.parent_id]
    return current.id


def _safe_status(
    contract, store, *, projection_context=None, priority_facts=None, query_context=None
) -> dict:
    status = project_task_status(
        contract,
        store,
        projection_context=projection_context,
        priority_facts=priority_facts,
        query_context=query_context,
    )
    return {
        "contract_id": contract.id,
        "name": contract.name,
        "status": status["status"],
        "terminal": status["terminal"],
        "ok": status["ok"],
        "outputs_satisfied": status["outputs_satisfied"],
        "blockers": list(status["blockers"]),
        "budget_remaining": status["budget_remaining"],
        "asserted_outputs": dict(status["outputs"]),
        "output_semantics": "task-view asset-name assertions; not acceptance qualification",
        "recoveries": list(status["recoveries"]),
        "rejection_count": status["rejection_count"],
        "trace_event_count": status["trace_event_count"],
        "assignment": status["assignment"],
        "priority": status["priority"],
        "priority_fact_id": status["priority_fact_id"],
    }


def _cards(
    store, contracts, query_context, priority_histories_by_contract
) -> list[dict]:
    by_id = {contract.id: contract for contract in contracts}
    cards = []
    for contract in sorted(contracts, key=lambda item: item.id):
        status = _safe_status(
            contract,
            store,
            projection_context=query_context.effect_projection,
            priority_facts=priority_histories_by_contract[contract.id],
            query_context=query_context,
        )
        cards.append(
            {
                "contract_id": contract.id,
                "root_contract_id": _root_id(contract, by_id),
                "parent_id": contract.parent_id,
                "name": contract.name,
                "status": status["status"],
                "terminal": status["terminal"],
                "blockers": status["blockers"],
                "budget_remaining": status["budget_remaining"],
                "outputs_satisfied": status["outputs_satisfied"],
                "recoveries": status["recoveries"],
                "assignment": status["assignment"],
                "priority": status["priority"],
                "priority_fact_id": status["priority_fact_id"],
            }
        )
    return cards


def _select_cards(cards, contracts, *, statuses, root_id):
    by_id = {contract.id: contract for contract in contracts}
    if root_id is not None and root_id not in by_id:
        raise LookupError(f"Contract {root_id!r} does not exist")
    selected = cards
    if statuses:
        selected = [item for item in selected if item["status"] in statuses]
    if root_id is not None:
        selected_ids = contract_tree_ids(contracts, root_id)
        selected = [item for item in selected if item["contract_id"] in selected_ids]
    return selected


def _sort_cards(store, cards, sort_by: str) -> list[dict]:
    if sort_by == "declaration":
        return cards
    ids = order_task_contract_ids(store, tuple(item["contract_id"] for item in cards))
    by_id = {item["contract_id"]: item for item in cards}
    return [by_id[contract_id] for contract_id in ids]


def _fetch_cards(store, projection, contracts, revision: int) -> list[dict]:
    """Project the full board from one revision-bearing fact snapshot."""
    query_context = load_task_view_query_context(store)
    if query_context.revision != revision:
        raise TaskViewQueryMismatch("task board revision changed during query setup")
    source_contracts = {item.id: item for item in query_context.contracts}
    supplied_contracts = {item.id: item for item in contracts}
    if (
        len(supplied_contracts) != len(contracts)
        or supplied_contracts != source_contracts
    ):
        raise TaskViewQueryMismatch(
            "task board Contract index differs from its snapshot"
        )
    # ``contracts`` is the already-read Redis index from _snapshot_context.
    # Compare complete values without another Redis lookup or order assumption.
    del projection
    histories = task_priority_histories(
        query_context.effect_projection, tuple(contract.id for contract in contracts)
    )
    return _cards(store, contracts, query_context, histories)


@click.command("list")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice(
        (
            "ready",
            "blocked",
            "blocked_capability",
            "blocked_delegation",
            "blocked_assignment",
            "claimed",
            "submitted",
            "expanded",
            "completed",
            "failed",
            "cancelled",
            "unreachable",
            "stalled",
        )
    ),
    help="Filter by projected status (repeatable).",
)
@click.option(
    "--root",
    "root_id",
    default=None,
    help="Exact root Contract ID; include its descendants.",
)
@click.option(
    "--limit", type=click.IntRange(min=1, max=1000), default=50, show_default=True
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(("declaration", "priority")),
    default="declaration",
    show_default=True,
    help="Optional advisory ordering; eligibility and claim authority are unchanged.",
)
@click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
def task_list(
    statuses: tuple[str, ...],
    root_id: str | None,
    limit: int,
    sort_by: str,
    as_json: bool,
) -> None:
    """List task summaries from the Redis index and committed task facts."""
    context = None
    try:
        context = _snapshot_context()
        store, projection, contracts, revision = context
        cards = _fetch_cards(store, projection, contracts, revision)
        matching = _select_cards(cards, contracts, statuses=statuses, root_id=root_id)
        matching = _sort_cards(store, matching, sort_by)
        selected = matching[:limit]
    except LookupError as exc:
        _emit_failure(
            str(exc), context[3] if context else None, as_json, code="not_found"
        )
    except click.exceptions.Exit:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        _emit_failure(
            _safe_error("Task query unavailable", exc),
            context[3] if context else None,
            as_json,
            code="redis_unavailable",
        )
    finally:
        _close_context(context)
    if as_json:
        _emit_success(
            {
                "tasks": selected,
                "total": len(cards),
                "matching_total": len(matching),
                "limit": limit,
                "has_more": len(matching) > len(selected),
            },
            revision,
            True,
        )
    elif not selected:
        click.echo("No tasks found.")
    else:
        for item in selected:
            click.echo(
                f"{item['contract_id']}  {item['status']:<20} "
                f"{item['name']}  budget={item['budget_remaining']} "
                f"priority={item['priority']}"
            )
        if len(matching) > len(selected):
            click.echo(f"Showing {len(selected)} of {len(matching)} matching tasks.")


@click.command("board")
@click.option(
    "--root",
    "root_id",
    default=None,
    help="Exact root Contract ID; include its descendants.",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(("declaration", "priority")),
    default="declaration",
    show_default=True,
    help="Optional advisory ordering within each status column.",
)
@click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
def task_board(root_id: str | None, sort_by: str, as_json: bool) -> None:
    """Show a Kanban-style grouping of projected task states."""
    context = None
    try:
        context = _snapshot_context()
        store, projection, contracts, revision = context
        cards = _fetch_cards(store, projection, contracts, revision)
        selected = _select_cards(cards, contracts, statuses=(), root_id=root_id)
        selected = _sort_cards(store, selected, sort_by)
    except LookupError as exc:
        _emit_failure(
            str(exc), context[3] if context else None, as_json, code="not_found"
        )
    except click.exceptions.Exit:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        _emit_failure(
            _safe_error("Task query unavailable", exc),
            context[3] if context else None,
            as_json,
            code="redis_unavailable",
        )
    finally:
        _close_context(context)
    columns: dict[str, list[dict]] = {}
    for item in selected:
        columns.setdefault(item["status"], []).append(item)
    data = {"columns": dict(sorted(columns.items())), "total": len(selected)}
    if as_json:
        _emit_success(data, revision, True)
        return
    for status, rows in sorted(columns.items()):
        click.echo(f"{status} ({len(rows)})")
        for row in rows:
            click.echo(
                f"  {row['contract_id']}  {row['name']}  priority={row['priority']}"
            )


@click.command("stats")
@click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
def task_stats(as_json: bool) -> None:
    """Count tasks by projected status."""
    context = None
    try:
        context = _snapshot_context()
        store, projection, contracts, revision = context
        cards = _fetch_cards(store, projection, contracts, revision)
    except (OSError, RuntimeError, ValueError) as exc:
        _emit_failure(
            _safe_error("Task query unavailable", exc),
            context[3] if context else None,
            as_json,
            code="redis_unavailable",
        )
    finally:
        _close_context(context)
    counts: dict[str, int] = {}
    for item in cards:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    data = {"total": len(cards), "by_status": dict(sorted(counts.items()))}
    if as_json:
        _emit_success(data, revision, True)
    else:
        click.echo(f"tasks: {len(cards)}")
        for status, count in sorted(counts.items()):
            click.echo(f"{status:<24} {count}")


@click.command("show")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
def task_show(contract_id: str, as_json: bool) -> None:
    """Show task projection, Contract fields, children, and recovery IDs."""
    context = None
    try:
        context = _snapshot_context()
        store, _projection, contracts, revision = context
        by_id = {contract.id: contract for contract in contracts}
        contract = by_id.get(contract_id)
        if contract is None:
            _emit_failure(
                f"Contract {contract_id!r} does not exist",
                revision,
                as_json,
                code="not_found",
            )
        status = _safe_status(contract, store)
        data = {
            "task": status,
            "contract": {
                "contract_id": contract.id,
                "parent_id": contract.parent_id,
                "origin": contract.origin,
                "name": contract.name,
                "description": contract.description,
                "inputs": list(contract.inputs),
                "outputs": list(contract.outputs),
                "activation": contract.activation,
                "budget": contract.budget,
                "tool_scope": list(contract.tool_scope),
                "labels": list(contract.labels),
                "worker_capabilities": list(contract.worker_capabilities),
                "worker_pools": list(contract.worker_pools),
                "delegation_capabilities": list(contract.delegation_capabilities),
                "delegation_pools": list(contract.delegation_pools),
            },
            "children": sorted(
                item.id for item in contracts if item.parent_id == contract.id
            ),
            "recovery_contract_ids": status["recoveries"],
        }
    except click.exceptions.Exit:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        _emit_failure(
            _safe_error("Task query unavailable", exc),
            context[3] if context else None,
            as_json,
            code="redis_unavailable",
        )
    finally:
        _close_context(context)
    if as_json:
        _emit_success(data, revision, True)
    else:
        click.echo(
            f"{contract.id}  {status['status']}  {contract.name}  "
            f"priority={status['priority']}"
        )
        click.echo(f"parent: {contract.parent_id or '-'}")
        click.echo(f"inputs: {list(contract.inputs)}")
        click.echo(f"outputs: {list(contract.outputs)}")
        click.echo(f"activation: {contract.activation or '-'}")
        click.echo(
            f"budget: {contract.budget} (remaining {status['budget_remaining']})"
        )
        click.echo(f"blockers: {status['blockers']}")
        click.echo(f"children: {data['children']}")
        click.echo(f"recoveries: {data['recovery_contract_ids']}")


def _qualified_outputs(contract, store) -> list[dict]:
    if not all_outputs_satisfied(contract, store):
        raise ValueError("task outputs are not all committed and policy-qualified")
    contracts = store.get_all_contracts()
    contract_ids = contract_tree_ids(contracts, contract.id)
    selected: list[dict] = []
    policy = contract.acceptance_policy
    for output_name in contract.outputs:
        qualified_ids: set[str] | None = None
        if policy is not None and policy.get("mode") == "independent":
            qualified_ids = {
                str(record.payload.get("asset_id", ""))
                for _, record in store.scan_runtime_records(
                    record_type="output.qualified"
                )
                if record.payload.get("contract_id") == contract.id
                and record.payload.get("output_name") == output_name
            }
        candidates = []
        for asset in store.get_assets_by_name(output_name):
            if not is_business_output(asset, output_name):
                continue
            if asset.disclosure_view != "original" or asset.tombstoned:
                continue
            if not asset.promptable:
                continue
            if (
                contract.origin in {"system", "recovery"}
                and asset.created_by != contract.id
            ):
                continue
            if qualified_ids is not None and (
                asset.id not in qualified_ids or asset.created_by not in contract_ids
            ):
                continue
            candidates.append(asset)
        if not candidates:
            raise ValueError(f"output {output_name!r} has no exportable original Asset")
        asset = sorted(candidates, key=lambda item: item.id)[-1]
        selected.append(
            {
                "name": output_name,
                "asset_id": asset.id,
                "content_type": asset.content_type,
                "origin": asset.origin,
                "created_by": asset.created_by,
                "content_bytes": len(asset.content.encode("utf-8")),
                "content_hash": asset.content_hash,
                "_content": asset.content,
            }
        )
    return selected


@click.command("result")
@click.argument("contract_id")
@click.option(
    "--output",
    "output_names",
    multiple=True,
    help="Select a declared output name (repeatable).",
)
@click.option(
    "--to",
    "destination",
    type=click.Path(path_type=Path),
    default=None,
    help="Write one selected output without overwriting a file.",
)
@click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
def task_result(
    contract_id: str,
    output_names: tuple[str, ...],
    destination: Path | None,
    as_json: bool,
) -> None:
    """Inspect or export outputs after projected completion and policy qualification."""
    context = None
    try:
        context = _snapshot_context()
        store, _projection, contracts, revision = context
        contract = next((item for item in contracts if item.id == contract_id), None)
        if contract is None:
            _emit_failure(
                f"Contract {contract_id!r} does not exist",
                revision,
                as_json,
                code="not_found",
            )
        if not contract.outputs:
            _emit_failure(
                "task declares no business outputs",
                revision,
                as_json,
                code="no_outputs",
            )
        status = project_task_status(contract, store, check_integrity=True)
        if not status.get("integrity", {}).get("consistent", False):
            _emit_failure(
                "task fact and view integrity check failed",
                revision,
                as_json,
                code="integrity_failed",
            )
        if status["status"] != "completed" or not status["outputs_satisfied"]:
            _emit_failure(
                "task is not completed with all declared outputs satisfied",
                revision,
                as_json,
                code="not_completed",
            )
        outputs = _qualified_outputs(contract, store)
        declared = {item["name"] for item in outputs}
        if output_names and (
            len(set(output_names)) != len(output_names)
            or not set(output_names) <= declared
        ):
            _emit_failure(
                "--output must name each selected declared output at most once",
                revision,
                as_json,
                code="invalid_output",
                exit_code=2,
            )
        selected = [
            item for item in outputs if not output_names or item["name"] in output_names
        ]
        if destination is not None and len(selected) != 1:
            _emit_failure(
                "--to requires exactly one selected output; use --output NAME",
                revision,
                as_json,
                code="invalid_output",
                exit_code=2,
            )
        written = None
        if destination is not None:
            content = selected[0]["_content"]
            try:
                with destination.open("xb") as stream:
                    stream.write(content.encode("utf-8"))
            except FileExistsError:
                _emit_failure(
                    f"output path already exists: {destination}",
                    revision,
                    as_json,
                    code="output_exists",
                )
            except OSError:
                _emit_failure(
                    "could not write output file",
                    revision,
                    as_json,
                    code="output_write_failed",
                )
            written = str(destination)
        metadata = [
            {key: value for key, value in item.items() if key != "_content"}
            for item in selected
        ]
        data = {
            "contract_id": contract.id,
            "status": status["status"],
            "outputs": metadata,
            "written_path": written,
        }
    except click.exceptions.Exit:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        _emit_failure(
            _safe_error("Task result unavailable", exc),
            context[3] if context else None,
            as_json,
            code="result_unavailable",
        )
    finally:
        _close_context(context)
    if as_json:
        _emit_success(data, revision, True)
    else:
        for item in metadata:
            click.echo(
                f"{item['name']}  {item['asset_id']}  {item['content_bytes']} bytes"
            )
        if written:
            click.echo(f"Wrote output to {written}")


@click.command("cancel")
@click.argument("contract_ids", nargs=-1, required=True)
@click.option("--reason", required=True, help="Stable operator cancellation reason.")
@click.option(
    "--idempotency-key", default=None, help="Candidate replay key or bulk base key."
)
@click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
def task_cancel(
    contract_ids: tuple[str, ...],
    reason: str,
    idempotency_key: str | None,
    as_json: bool,
) -> None:
    """Cancel unfinished tasks by publishing signed Candidates."""
    from aigineering.cli._task_batch import (
        collect_task_batch,
        emit_batch,
        new_batch_key_base,
        preflight_task_batch,
        validate_batch_key,
        validate_task_ids,
    )
    from aigineering.cli._common import _persistent_store
    from aigineering.task_publication import (
        CancelPublicationRejected,
        publish_task_cancel,
    )

    try:
        ids = validate_task_ids(contract_ids)
        if not reason.strip():
            raise ValueError("--reason must not be empty or whitespace")
        reason.encode("utf-8", errors="strict")
        validate_batch_key(idempotency_key)
    except ValueError as exc:
        _emit_failure(
            str(exc),
            None,
            as_json,
            code="invalid_reason" if not reason.strip() else "invalid_input",
            exit_code=2,
        )
    if len(ids) == 1:
        store = persistent_store()
        revision = None
        try:
            collaboration_service(store).sync()
            data, revision = publish_task_cancel(
                store, ids[0], reason, idempotency_key=idempotency_key
            )
        except LookupError as exc:
            if type(exc) is LookupError:
                _emit_failure(str(exc), revision, as_json, code="not_found")
            _emit_internal_error(
                _safe_error("Task cancellation failed", exc), exc, revision, as_json
            )
        except CancelPublicationRejected as exc:
            if as_json:
                _output_json(
                    {
                        "ok": False,
                        "error": {
                            "code": "cancel_rejected",
                            "message": str(exc),
                        },
                        "candidate_accepted": False,
                        "facts_retained": True,
                        "data": exc.data,
                        "revision": exc.revision,
                    }
                )
                raise click.exceptions.Exit(1) from exc
            raise click.ClickException(
                _cancel_rejection_human_message(exc.data, str(exc))
            ) from exc
        except ValueError as exc:
            _emit_internal_error(
                _safe_error("Task cancellation failed", exc), exc, revision, as_json
            )
        except RedisSyncPending as exc:
            if as_json:
                has_operation_result = exc.decision is not None or exc.data is not None
                if not has_operation_result:
                    _output_json(
                        {
                            "ok": False,
                            "error": {
                                "code": "redis_sync_pending",
                                "message": (
                                    "Redis synchronization failed before "
                                    "cancellation "
                                    "publication; no operation was attempted."
                                ),
                                "facts_retained": False,
                            },
                        }
                    )
                    raise click.exceptions.Exit(1) from exc
                operation_revision = exc.revision
                if type(operation_revision) is not int:
                    operation_revision = store.get_runtime_revision()
                _output_json(
                    {
                        "ok": False,
                        "error": {
                            "code": "redis_sync_pending",
                            "message": str(exc),
                            "facts_retained": True,
                            "candidate_accepted": exc.accepted,
                        },
                        "data": exc.data,
                        "revision": operation_revision,
                    }
                )
                raise click.exceptions.Exit(1) from exc
            message = str(exc)
            if exc.decision is None and exc.data is None:
                message = (
                    "Redis synchronization failed before cancellation "
                    "publication; "
                    "no operation was attempted."
                )
            if exc.accepted is False:
                message = _cancel_rejection_human_message(exc.data, message)
            raise click.ClickException(message) from exc
        except QueryProjectionUnavailable:
            raise
        except Exception as exc:
            _emit_internal_error(
                _safe_error("Task cancellation failed", exc), exc, revision, as_json
            )
        finally:
            store.close()
        if as_json:
            _emit_success(data, revision, True)
        else:
            click.echo(f"Task cancelled: {ids[0]} ({reason.strip()})")
        return
    base = idempotency_key or new_batch_key_base("cancel")
    try:
        preflight_task_batch(lambda: _persistent_snapshot())
    except QueryProjectionUnavailable as exc:
        _emit_failure(
            _safe_error("Task batch preflight failed", exc),
            None,
            as_json,
            code="redis_unavailable",
        )
    except FileNotFoundError:
        _emit_failure(
            "Workspace Store missing; initialize this workspace first.",
            None,
            as_json,
            code="workspace_uninitialized",
        )
    except Exception as exc:
        _emit_internal_error(
            _safe_error("Task batch preflight failed", exc), exc, None, as_json
        )

    def invoke(contract_id, key):
        store = _persistent_store()
        try:
            return publish_task_cancel(store, contract_id, reason, idempotency_key=key)
        finally:
            store.close()

    collection = collect_task_batch(
        ids,
        action="cancel",
        batch_key_base=base,
        invoke=invoke,
        expected_rejection_types=(CancelPublicationRejected,),
        rejection_result=lambda exc: (exc.data, exc.revision),
        include_operation_revision=True,
    )
    emit_batch(collection, as_json=as_json, emit_json=_output_json)


def _cancel_rejection_human_message(data: object, message: str) -> str:
    """Add a verified durable rejection reference to human cancellation errors."""
    if not isinstance(data, dict):
        return message
    record_id = data.get("rejection_record_id")
    if (
        isinstance(record_id, str)
        and len(record_id) == len("record:") + 64
        and record_id.startswith("record:")
        and all(character in "0123456789abcdef" for character in record_id[7:])
    ):
        return f"{message} (rejection audit record {record_id})"
    return message


@click.command("retry")
@click.argument("contract_ids", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
def task_retry(contract_ids: tuple[str, ...], as_json: bool) -> None:
    """Publish a signed security-equivalent retry Contract."""
    if len(contract_ids) > 1:
        _task_retry_batch(contract_ids, as_json)
        return
    contract_id = contract_ids[0]
    store = None
    try:
        require_redis()
        store = persistent_store()
        projection = query_projection(store)
        collaboration_service(store).sync()
        original = projection.get_contract(contract_id)
        if original is None:
            raise LookupError(f"Contract {contract_id!r} does not exist")
        data, revision = retry_local_task_result(store, contract_id)
    except Exception as exc:
        emit_retry_error(exc, as_json=as_json)
    finally:
        if store is not None:
            store.close()
    emit_retry_success(data, revision, as_json=as_json)


def _task_retry_batch(contract_ids: tuple[str, ...], as_json: bool) -> None:
    try:
        ids = validate_task_ids(contract_ids)
    except ValueError as exc:
        _emit_failure(str(exc), None, as_json, code="invalid_input")
        return
    correlation_id = new_batch_key_base("retry-correlation")
    try:
        preflight_task_batch(_persistent_snapshot)
    except FileNotFoundError:
        _emit_failure(
            "Initialize this workspace before retrying tasks.",
            None,
            as_json,
            code="workspace_not_initialized",
        )
    except (click.ClickException, click.exceptions.Exit):
        raise
    except (RedisSyncPending, QueryProjectionUnavailable):
        _emit_failure(
            "Required Redis collaboration is unavailable.",
            None,
            as_json,
            code="redis_unavailable",
        )
    except Exception as exc:
        _emit_internal_error(
            _safe_error("Task batch preflight failed", exc), exc, None, as_json
        )

    def invoke(contract_id: str, _key: str | None) -> tuple[dict, int]:
        store = persistent_store()
        try:
            return retry_local_task_result(store, contract_id)
        finally:
            store.close()

    collection = collect_task_batch(
        ids,
        action="retry",
        batch_key_base=correlation_id,
        key_for_target=lambda _contract_id: None,
        invoke=invoke,
        expected_rejection_types=(RetryPublicationRejected,),
        rejection_result=lambda exc: (exc.data, exc.revision),
        include_operation_revision=True,
        batch_field="batch_correlation_id",
    )
    emit_batch(collection, as_json=as_json, emit_json=_output_json)


# Bulk retry validates the entire target set before required Redis access.
# The single- and multi-target callback branches preflight explicitly.
task_retry.callback._redis_protected = True
