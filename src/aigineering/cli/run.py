"""aig run and aig demo commands."""

from __future__ import annotations

import json
import math
import shlex
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click

from aigineering.application import collaboration_service
from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.runtime_services import (
    sync_runtime,
)

from aigineering.configured_run import (
    publish_tool_descriptors as _publish_tool_descriptors,
    run_configured,
)
from aigineering.local_fleet import FleetHost, _claim_cycle_slot, run_local_cycle
from aigineering.run_config import prepare_configured_run

from aigineering.agent.tool_registry_loader import (
    load_tool_registry,
    provider_tool_definitions,
)
from aigineering.agent.tool_worker import ToolWorker

from aigineering.cli._common import (
    _asset_names_for,
    _contract_tree_ids,
    _default_completion_registry,
    _get_trace_dir,
    _load_quick_goal_inputs,
    _output_json,
    _persistent_store,
    _run_demo,
    _session_id,
)
from aigineering.cli._cycle_result import configured_cycle_payload
from aigineering.task_views import (
    project_task_status,
    recovery_successor_map,
)
from aigineering.local_identity import (
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
)
from aigineering.application import build_worker, validate_worker_id
from aigineering.runtime import (
    WorkerInvocationError,
    process_task_completions,
    run_runtime_maintenance_step,
)
from aigineering.task_logs import execute_captured_package
from aigineering.run_failure_diagnostics import (
    format_worker_failure_detail,
    invocation_failure_detail,
    safe_task_rejections,
    safe_worker_error_messages,
)
from aigineering.core.session import SessionStore
from aigineering.core.session import runtime_record_contract_references
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import Session


def _parse_capabilities(capabilities_str: Optional[str]) -> frozenset[str] | None:
    """Parse a comma-separated capabilities string into a frozenset."""
    if capabilities_str is None:
        return None
    parts = [c.strip() for c in capabilities_str.split(",") if c.strip()]
    return frozenset(parts) if parts else None


@dataclass(frozen=True)
class _PoolHost:
    host: object
    required_contract_capability: str | None = None


def _output_run_json(
    contract_id: str,
    trace_ids: list[str],
    session_id: str,
    task_status: dict,
    outputs: list[dict] | None = None,
    output_path: str | None = None,
    delivery_error: str | None = None,
) -> None:
    _output_json(
        {
            "contract_id": contract_id,
            "session_id": session_id,
            "trace_ids": trace_ids,
            "status": (
                "output_error"
                if task_status["ok"] and delivery_error
                else "complete"
                if task_status["ok"]
                else task_status["status"]
            ),
            "ok": task_status["ok"] and delivery_error is None,
            "blockers": task_status["blockers"],
            "outputs": outputs or [],
            "output_path": output_path,
            "error": delivery_error,
            "rejections": task_status.get("rejections", []),
            "recoveries": task_status.get("recoveries", []),
            "failure_details": task_status.get("failure_details", []),
            "capture_warnings": task_status.get("capture_warnings", []),
            "next_step": (
                None
                if task_status["ok"] and delivery_error is None
                else (
                    "The task completed and its Asset is committed; inspect it with "
                    "aig asset show <output_slot> --json and retry with a new --output path."
                    if task_status["ok"]
                    else (
                        f"Inspect with: aig task audit {contract_id} --json; "
                        "run a listed recovery explicitly with "
                        "aig run --task <contract_id>."
                    )
                )
            ),
        }
    )


def _quick_output_exportable(asset) -> bool:
    """Return whether a quick-goal result may leave the local Store as text."""
    return (
        asset.promptable
        and asset.disclosure_view == "original"
        and not asset.tombstoned
    )


@click.command("run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Run a packaged scenario or resume a configured Worker fleet.",
)
@click.argument("goal", required=False)
@click.option(
    "--once",
    "run_once",
    is_flag=True,
    default=False,
    help="Claim and execute at most one ready package from the local task pool.",
)
@click.option(
    "--task",
    "target_task_id",
    default=None,
    help="Run worker cycles until this task reaches a terminal status.",
)
@click.option(
    "--resume",
    "resume_task_id",
    default=None,
    help="Resume a task; with --config, use its configured Worker fleet.",
)
@click.option(
    "--wait-timeout",
    type=float,
    default=60.0,
    show_default=True,
    help="Maximum seconds to wait when --task is used.",
)
@click.option(
    "--interval",
    type=float,
    default=1.0,
    show_default=True,
    help="Polling interval when waiting for --task.",
)
@click.option(
    "--mock-output",
    default=None,
    help="Explicit mock worker output for deterministic dry runs.",
)
@click.option(
    "--mock-preset",
    "mock_presets",
    multiple=True,
    help="Explicit mock preset as contract_name=raw_output (repeatable).",
)
@click.option(
    "--worker",
    "worker_kind",
    type=click.Choice(["mock", "llm"]),
    default="llm",
    show_default=True,
    help="Worker implementation; mock is only for explicit tests and dry runs.",
)
@click.option(
    "--worker-id",
    default=None,
    help="Durable execution identity; use a separate ID for an independent reviewer.",
)
@click.option(
    "--model",
    default=None,
    envvar="AIGINEERING_MODEL",
    help="LLM model name (or AIGINEERING_MODEL).",
)
@click.option(
    "--base-url",
    default="https://api.openai.com/v1",
    envvar="AIGINEERING_BASE_URL",
    show_default=True,
    help="OpenAI-compatible base URL when --worker llm.",
)
@click.option(
    "--timeout",
    type=float,
    default=60.0,
    show_default=True,
    help="Request timeout in seconds.",
)
@click.option(
    "--max-retries",
    type=click.IntRange(min=0),
    default=3,
    show_default=True,
    help="Maximum retries for transient errors.",
)
@click.option(
    "--max-output-tokens",
    type=click.IntRange(min=1),
    default=2048,
    show_default=True,
    help="Maximum output tokens per provider response.",
)
@click.option(
    "--thinking-mode",
    type=click.Choice(["enabled", "disabled"]),
    default=None,
    help="Provider thinking mode; omitted leaves the provider default unchanged.",
)
@click.option(
    "--response-format",
    type=click.Choice(["json_object"]),
    default=None,
    help="Provider response wire format; omitted leaves provider defaults unchanged.",
)
@click.option(
    "--capability",
    "capabilities_str",
    default=None,
    help="Comma-separated provider capabilities (e.g. tool_calling,json_schema).",
)
@click.option(
    "--tool-registry",
    default=None,
    metavar="MODULE:FACTORY",
    help="Explicit local ToolRegistry factory; loads trusted operator code.",
)
@click.option(
    "--behavior-label",
    "behavior_labels",
    multiple=True,
    default=(),
    help="Behavior label to inject (repeatable).",
)
@click.option(
    "--save-config",
    "save_config",
    is_flag=True,
    default=False,
    help="Persist a sealed provider config snapshot before running.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
@click.option(
    "--input",
    "input_specs",
    multiple=True,
    metavar="NAME=PATH",
    help="UTF-8 input file to disclose to this quick goal (repeatable).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Markdown result path (default: .aig/outputs/<session>.md).",
)
@click.pass_context
def run(
    ctx: click.Context,
    config_path: Path | None,
    goal: Optional[str],
    run_once: bool,
    target_task_id: Optional[str],
    resume_task_id: str | None,
    wait_timeout: float,
    interval: float,
    mock_output: Optional[str],
    mock_presets: tuple[str, ...],
    worker_kind: str,
    worker_id: str | None,
    model: Optional[str],
    base_url: str,
    timeout: float,
    max_retries: int,
    max_output_tokens: int,
    thinking_mode: str | None,
    response_format: str | None,
    capabilities_str: Optional[str],
    tool_registry: Optional[str],
    behavior_labels: tuple[str, ...],
    save_config: bool,
    json_output: bool,
    input_specs: tuple[str, ...],
    output_path: Path | None,
) -> None:
    """Run a packaged scenario, resume a task, or run a quick goal/task cycle."""
    if worker_id is not None:
        try:
            validate_worker_id(worker_id)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--worker-id") from exc
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise click.BadParameter(
            "must be a finite positive number", param_hint="--wait-timeout"
        )
    if not math.isfinite(interval) or interval <= 0:
        raise click.BadParameter(
            "must be a finite positive number", param_hint="--interval"
        )
    if not math.isfinite(timeout) or timeout <= 0:
        raise click.BadParameter(
            "must be a finite positive number", param_hint="--timeout"
        )
    if resume_task_id is not None and target_task_id is not None:
        raise click.UsageError("Choose only one of --task and --resume.")
    if target_task_id is not None and not target_task_id.strip():
        raise click.BadParameter("must not be empty or whitespace", param_hint="--task")
    if resume_task_id is not None and not resume_task_id.strip():
        raise click.BadParameter(
            "must not be empty or whitespace", param_hint="--resume"
        )
    selected_resume = resume_task_id if resume_task_id is not None else target_task_id
    if config_path is not None:
        allowed_with_config = {
            "config_path",
            "target_task_id",
            "resume_task_id",
            "wait_timeout",
            "json_output",
            "run_once",
        }
        explicit = [
            name
            for name in ctx.params
            if name not in allowed_with_config
            and ctx.get_parameter_source(name) == click.core.ParameterSource.COMMANDLINE
        ]
        if explicit:
            display = {
                "goal": "GOAL",
                "run_once": "--once",
                "interval": "--interval",
                "input_specs": "--input",
                "output_path": "--output",
                "mock_output": "--mock-output",
                "mock_presets": "--mock-preset",
                "worker_kind": "--worker",
                "worker_id": "--worker-id",
                "model": "--model",
                "base_url": "--base-url",
                "timeout": "--timeout",
                "max_retries": "--max-retries",
                "max_output_tokens": "--max-output-tokens",
                "thinking_mode": "--thinking-mode",
                "response_format": "--response-format",
                "capabilities_str": "--capability",
                "tool_registry": "--tool-registry",
                "behavior_labels": "--behavior-label",
                "save_config": "--save-config",
            }
            raise click.UsageError(
                "--config cannot be combined with "
                + ", ".join(
                    display.get(name, f"--{name.replace('_', '-')}")
                    for name in explicit
                )
                + "."
            )
        try:
            prepared = prepare_configured_run(
                config_path, resume_task_id=selected_resume
            )
            result = (
                run_configured(prepared, timeout=wait_timeout, once=True)
                if run_once
                else run_configured(prepared, timeout=wait_timeout)
            )
        except QueryProjectionUnavailable:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        if hasattr(result, "stop_reason"):
            payload = configured_cycle_payload(
                result,
                resume_command=shlex.join(
                    [
                        "aig",
                        "run",
                        "--config",
                        str(Path(config_path).resolve()),
                        "--resume",
                        result.contract_id,
                        "--once",
                        "--json",
                    ]
                ),
            )
        else:
            payload = {
                "contract_id": result.contract_id,
                "status": result.status,
                "completed": result.completed,
                "timed_out": result.timed_out,
                "worker_errors": safe_worker_error_messages(
                    result.worker_errors, result.worker_failure_details
                ),
                "worker_failure_details": list(result.worker_failure_details),
                "capture_warnings": list(result.capture_warnings),
            }
        if json_output:
            _output_json(payload)
        else:
            if hasattr(result, "stop_reason"):
                _emit_run_result(payload, False)
            else:
                click.echo(f"run {result.status}: {result.contract_id}")
                for error in safe_worker_error_messages(
                    result.worker_errors, result.worker_failure_details
                ):
                    click.echo(f"worker error: {error}")
                for detail in result.worker_failure_details:
                    click.echo(
                        "worker failure detail: "
                        + json.dumps(detail, ensure_ascii=True, sort_keys=True)
                    )
            for warning in result.capture_warnings:
                click.echo(f"capture warning: {warning}")
        if not result.completed:
            raise click.exceptions.Exit(1)
        return
    if resume_task_id is not None:
        target_task_id = resume_task_id
    modes = int(goal is not None) + int(run_once) + int(target_task_id is not None)
    if modes != 1:
        raise click.UsageError(
            "Choose exactly one of GOAL, --once, or --task <contract_id>."
        )
    if goal is not None and not goal.strip():
        raise click.BadParameter("must not be empty or whitespace", param_hint="GOAL")
    if response_format is not None and worker_kind != "llm":
        raise click.UsageError("--response-format requires --worker llm.")
    if response_format is not None and tool_registry is not None:
        raise click.UsageError(
            "--response-format cannot be combined with --tool-registry."
        )
    if (input_specs or output_path is not None) and goal is None:
        raise click.UsageError("--input and --output can only be used with GOAL.")
    if goal is not None and (tool_registry or mock_output is not None or mock_presets):
        raise click.UsageError(
            "--tool-registry, --mock-output, and --mock-preset are task-pool options; "
            "quick GOAL runs do not execute operator tools or accept mock overrides."
        )
    if goal is None and (behavior_labels or save_config):
        raise click.UsageError(
            "--behavior-label and --save-config can only be used with GOAL."
        )
    capabilities = _parse_capabilities(capabilities_str)
    if run_once or target_task_id:
        _run_task_pool(
            target_task_id=target_task_id,
            worker_kind=worker_kind,
            worker_id=worker_id,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            thinking_mode=thinking_mode,
            response_format=response_format,
            capabilities=capabilities,
            tool_registry_reference=tool_registry,
            mock_output=mock_output,
            mock_presets=mock_presets,
            wait_timeout=wait_timeout,
            interval=interval,
            json_output=json_output,
        )
        return

    session_id = _session_id()
    quick_inputs, input_description = _load_quick_goal_inputs(
        goal, input_specs, session_id.removeprefix("session_")
    )
    selected_output_path = output_path or (
        _get_trace_dir().parent / "outputs" / f"{session_id}.md"
    )
    if selected_output_path.exists() or selected_output_path.is_symlink():
        raise click.BadParameter(
            f"output path already exists: {selected_output_path}", param_hint="--output"
        )
    trace_path = _get_trace_dir() / f"{session_id}.jsonl"
    jsonl_store = JsonLTraceStore(str(trace_path))
    try:
        store, trace_store, contract, scope = _run_demo(
            goal,
            trace_store=jsonl_store,
            store=_persistent_store(),
            capture_db_path=".aig/store.db",
            worker_kind=worker_kind,
            worker_id=worker_id,
            model=model,
            base_url=base_url,
            save_config=save_config,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            thinking_mode=thinking_mode,
            response_format=response_format,
            capabilities=capabilities,
            behavior_labels=behavior_labels,
            quick_inputs=quick_inputs,
            quick_input_description=input_description,
            raise_on_incomplete=False,
        )
    except QueryProjectionUnavailable:
        raise
    except (RuntimeError, WorkerInvocationError) as exc:
        raise click.ClickException(str(exc)) from exc
    entries = trace_store.get_by_contract(contract.id)
    trace_ids = list(scope.trace_ids)

    # ── Session manifest ───────────────────────────────────────────────────
    session = Session(
        id=session_id,
        root_contract_id=contract.id,
        contract_ids=scope.contract_ids,
        asset_ids=scope.asset_ids,
        trace_ids=trace_ids,
        scope="root_tree",
        execution_start_revision=scope.start_revision,
        execution_end_revision=scope.end_revision,
        produced_asset_ids=scope.produced_asset_ids,
        unassigned_trace_ids=scope.unassigned_trace_ids,
    )
    session_store = SessionStore()
    session_store.create_session(session)
    task_status = project_task_status(contract, store)
    task_status["capture_warnings"] = list(scope.capture_warnings)
    outputs: list[dict] = []
    markdown_content: str | None = None
    for slot, asset_id in task_status["outputs"].items():
        asset = store.get_asset(asset_id)
        if asset is None:
            continue
        exportable = _quick_output_exportable(asset)
        output = {"slot": slot, "asset_id": asset_id, "exportable": exportable}
        if exportable:
            output["content"] = asset.content
            markdown_content = asset.content
        outputs.append(output)

    delivery_error = None
    written_output_path = None
    if task_status["ok"] and markdown_content is not None:
        try:
            selected_output_path.parent.mkdir(parents=True, exist_ok=True)
            with selected_output_path.open("x", encoding="utf-8") as stream:
                stream.write(markdown_content)
                if not markdown_content.endswith("\n"):
                    stream.write("\n")
            written_output_path = str(selected_output_path)
        except OSError as exc:
            delivery_error = (
                f"could not save Markdown output: {exc.strerror or type(exc).__name__}"
            )
    elif task_status["ok"]:
        delivery_error = "completed task has no exportable text output"

    if json_output:
        reported_error = delivery_error
        if not task_status["ok"]:
            reported_error = f"goal run ended with status {task_status['status']}"
        _output_run_json(
            contract_id=contract.id,
            trace_ids=trace_ids,
            session_id=session_id,
            task_status=task_status,
            outputs=outputs,
            output_path=written_output_path,
            delivery_error=reported_error,
        )
        if not task_status["ok"] or delivery_error:
            raise click.exceptions.Exit(1)
        return

    for entry in entries:
        if entry.event_type == "activation":
            click.echo(f"✓ contract {contract.name} activated")
        elif entry.event_type == "disclosure":
            names = _asset_names_for(entry.disclosed_assets, store)
            worker_id = entry.worker_id or "unknown_worker"
            click.echo(f"→ disclosed {names} to {worker_id}")
        elif entry.event_type == "projection":
            total = len(entry.accepted_fragments) + len(entry.rejected_fragments)
            click.echo(f"→ worker produced {total} candidates")
            for name in entry.rejected_fragments:
                click.echo(f"✗ '{name}' REJECTED")
            for aid in entry.accepted_fragments:
                asset = store.get_asset(aid)
                name = asset.name if asset else aid
                click.echo(f"✓ '{name}' accepted and committed")
        elif entry.event_type == "complete":
            click.echo("✓ contract complete")

    click.echo(f"Trace saved to {trace_path}")
    if not task_status["ok"]:
        click.echo(
            f"Run ended without satisfied outputs: {task_status['status']}; "
            f"blockers={task_status['blockers']}"
        )
        if task_status["rejections"]:
            click.echo(f"Rejected output: {task_status['rejections']}")
        if task_status["recoveries"]:
            click.echo(f"Recovery successors: {task_status['recoveries']}")
        click.echo(f"Inspect with: aig task audit {contract.id} --json")
        raise click.exceptions.Exit(1)
    if delivery_error:
        click.echo(f"Run completed, but result export failed: {delivery_error}")
        raise click.exceptions.Exit(1)
    click.echo(f"Markdown output saved to {written_output_path}")
    for output in outputs:
        click.echo(f"Output {output['slot']}: {output['asset_id']}")


def _run_task_pool(
    *,
    target_task_id: str | None,
    worker_kind: str,
    model: str | None,
    base_url: str,
    timeout: float,
    max_retries: int,
    capabilities: frozenset[str] | None,
    tool_registry_reference: str | None,
    mock_output: str | None,
    mock_presets: tuple[str, ...],
    wait_timeout: float,
    interval: float,
    json_output: bool,
    max_output_tokens: int | None = None,
    thinking_mode: str | None = None,
    response_format: str | None = None,
    worker_id: str | None = None,
) -> None:
    store = _persistent_store()
    cycles: list[dict] = []
    capture_warnings: list[str] = []
    worker_failure_details: list[dict] = []
    try:
        tool_registry = (
            load_tool_registry(tool_registry_reference)
            if tool_registry_reference is not None
            else None
        )
        provider_capabilities = capabilities or frozenset()
        if tool_registry is not None:
            provider_capabilities = provider_capabilities | {"tool_calling"}
            _publish_tool_descriptors(store, tool_registry)
        worker_options = {}
        if worker_id is not None:
            worker_options["worker_id"] = worker_id
        if max_output_tokens is not None:
            worker_options["max_output_tokens"] = max_output_tokens
        if thinking_mode is not None:
            worker_options["thinking_mode"] = thinking_mode
        if response_format is not None:
            worker_options["response_format"] = response_format
        worker = build_worker(
            worker_kind,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            capabilities=provider_capabilities,
            tool_definitions=(
                provider_tool_definitions(tool_registry)
                if tool_registry is not None
                else None
            ),
            **worker_options,
        )
        _configure_pool_worker(
            worker,
            store,
            worker_kind=worker_kind,
            mock_output=mock_output,
            mock_presets=mock_presets,
        )
        hosts = [_PoolHost(ensure_local_worker_host(store, worker))]
        if tool_registry is not None:
            hosts.append(
                _PoolHost(
                    ensure_local_worker_host(store, ToolWorker(tool_registry)),
                    "tool-execution",
                )
            )
        deadline = time.monotonic() + wait_timeout
        candidate_publishers = ensure_local_runtime_publishers(store)
        registry = _default_completion_registry()
        if target_task_id is None:
            result = run_local_cycle(
                store,
                tuple(
                    FleetHost(
                        host=item.host,
                        required_contract_capability=item.required_contract_capability,
                    )
                    for item in hosts
                ),
                db_path=".aig/store.db",
                timeout=wait_timeout,
            )
            if result.projection is None:
                _emit_run_result(
                    {
                        "ok": False,
                        "status": "idle",
                        "error": "No enabled unclaimed contract is available.",
                        "cycles": [],
                    },
                    json_output,
                )
                raise click.exceptions.Exit(1)
            status = result.projection
            status.update(
                ok=result.completed,
                cycles=(
                    []
                    if result.stop_reason == "worker_failure"
                    else list(result.cycles)
                ),
                capture_warnings=list(result.capture_warnings),
                worker_failure_details=list(result.worker_failure_details),
                worker_errors=safe_worker_error_messages(
                    result.worker_errors, result.worker_failure_details
                ),
                stop_reason=result.stop_reason,
            )
            if result.submission_status is not None:
                status["submission_status"] = result.submission_status
            status["rejections"] = safe_task_rejections(
                status.get("rejections", ()), result.worker_failure_details
            )
            safe_errors = safe_worker_error_messages(
                result.worker_errors, result.worker_failure_details
            )
            if safe_errors:
                status["error"] = "; ".join(safe_errors)
            _emit_run_result(status, json_output)
            if not result.completed:
                raise click.exceptions.Exit(1)
            return

        _run_target_pool(
            store,
            hosts,
            candidate_publishers,
            registry,
            target_task_id=target_task_id,
            deadline=deadline,
            interval=interval,
            cycles=cycles,
            json_output=json_output,
            capture_warnings=capture_warnings,
            worker_failure_details=worker_failure_details,
        )
    except WorkerInvocationError:
        sync_runtime(store)
        failure_message = (
            "; ".join(
                format_worker_failure_detail(detail)
                for detail in worker_failure_details
            )
            if worker_failure_details
            else "worker invocation failed; exact claim diagnostics are unavailable"
        )
        failure = {
            "ok": False,
            "status": "failed",
            "error": failure_message,
            "cycles": cycles,
            "capture_warnings": capture_warnings,
            "worker_failure_details": worker_failure_details,
        }
        if target_task_id is not None:
            target = store.get_contract(target_task_id)
            if target is not None:
                failure.update(project_task_status(target, store))
                failure["rejections"] = safe_task_rejections(
                    failure.get("rejections", ()), worker_failure_details
                )
                failure["ok"] = False
                failure["error"] = failure_message
                tree_ids = _contract_tree_ids(store.get_all_contracts(), target_task_id)
                failure["failure_categories"] = _worker_failure_categories(
                    store, tree_ids
                )
                all_contracts = store.get_all_contracts()
                contracts_by_id = {contract.id: contract for contract in all_contracts}
                recovery_map = recovery_successor_map(store, all_contracts)
                recovery_ids = sorted(
                    {
                        recovery_id
                        for failed_id in tree_ids
                        if failed_id in contracts_by_id
                        for recovery_id in recovery_map.get(failed_id, ())
                    }
                )
                failure["recoveries"] = sorted(
                    set(failure.get("recoveries", ())) | set(recovery_ids)
                )
                failure["recovery_count"] = len(failure["recoveries"])
                failure["stop_reason"] = "worker_failure"
                failure["next_step"] = (
                    "Inspect aig task audit for the recorded failure category and "
                    "recovery; run a recovery successor explicitly if one was published."
                )
        _emit_run_result(failure, json_output)
        raise click.exceptions.Exit(1)
    except ValueError as e:
        raise click.ClickException(str(e))
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _configure_pool_worker(
    worker,
    store,
    *,
    worker_kind: str,
    mock_output: str | None,
    mock_presets: tuple[str, ...],
) -> None:
    set_output = getattr(worker, "set_output", None)
    if mock_output is not None or mock_presets:
        if worker_kind != "mock":
            raise click.ClickException(
                "--mock-output/--mock-preset requires --worker mock"
            )
        if set_output is None:
            return
        if mock_output is not None:
            for contract in store.get_all_contracts():
                set_output(contract.name, mock_output)
        for preset in mock_presets:
            name, sep, output = preset.partition("=")
            if sep != "=" or not name:
                raise click.ClickException(
                    "--mock-preset must use contract_name=raw_output"
                )
            set_output(name, output)
        return
    if worker_kind != "mock" or set_output is None:
        return
    for contract in store.get_all_contracts():
        outputs = {
            name: f"Deterministic mock output for {contract.name}:{name}"
            for name in contract.outputs
        }
        set_output(
            contract.name,
            "/exec " + json.dumps({"outputs": outputs}, sort_keys=True),
        )


def _run_target_pool(
    store,
    hosts,
    candidate_publishers,
    registry,
    *,
    target_task_id: str,
    deadline: float,
    interval: float,
    cycles: list[dict],
    json_output: bool,
    capture_warnings: list[str],
    worker_failure_details: list[dict] | None = None,
) -> None:
    worker_failure_details = (
        worker_failure_details if worker_failure_details is not None else []
    )
    while True:
        target = store.get_contract(target_task_id)
        if target is None:
            _emit_run_result(
                {
                    "ok": False,
                    "status": "error",
                    "error": f"Task '{target_task_id}' not found.",
                    "cycles": cycles,
                },
                json_output,
            )
            raise click.exceptions.Exit(1)
        sync_runtime(store)
        status = project_task_status(target, store)
        if status["terminal"]:
            _finish_target_run(
                status,
                cycles,
                stop_reason="terminal",
                json_output=json_output,
                capture_warnings=capture_warnings,
            )
            return

        before_revision = store.get_runtime_revision()
        maintenance = run_runtime_maintenance_step(
            store, registry, candidate_publishers=candidate_publishers
        )
        recovered = list(maintenance.rejected_submissions)
        processed_before = list(maintenance.task_completions)
        claimed_packages = []
        submissions = []
        for pool_host in hosts:
            claimed = _claim_for_target_pool_host(
                store,
                pool_host,
                candidate_publishers,
                target_task_id=target_task_id,
            )
            if claimed is None:
                continue
            claimed_packages.append(claimed)
            try:
                submissions.append(
                    execute_captured_package(
                        claimed,
                        pool_host.host,
                        store,
                        db_path=".aig/store.db",
                        candidate_publishers=candidate_publishers,
                        warnings=capture_warnings,
                    )
                )
            except WorkerInvocationError:
                worker_failure_details.append(invocation_failure_detail(store, claimed))
                raise
        processed_after = process_task_completions(
            store, registry, candidate_publishers=candidate_publishers
        )
        end_revision = store.get_runtime_revision()
        window_records = list(
            store.scan_runtime_records(
                after_revision=before_revision,
                through_revision=end_revision,
            )
        )
        touched_contracts, unassigned_trace_ids, trace_count = _cycle_trace_scope(
            window_records
        )
        if trace_count:
            cycles.append(
                {
                    "contracts": touched_contracts,
                    "unassigned_trace_ids": unassigned_trace_ids,
                    "trace_events": trace_count,
                    "submission_status": (
                        submissions[-1].get("status") if submissions else None
                    ),
                    "tasks_processed": processed_before + processed_after,
                    "rejections_recovered": recovered,
                }
            )

        target = store.get_contract(target_task_id)
        assert target is not None
        sync_runtime(store)
        status = project_task_status(target, store)
        if status["terminal"]:
            _finish_target_run(
                status,
                cycles,
                stop_reason="terminal",
                json_output=json_output,
                capture_warnings=capture_warnings,
            )
            return

        timed_out = time.monotonic() >= deadline
        idle = (
            not claimed_packages
            and not processed_before
            and not processed_after
            and not recovered
        )
        if timed_out or idle:
            if status.get("silent_failure_risks"):
                status["status"] = "stalled"
            status["timed_out"] = timed_out
            status["capture_warnings"] = list(capture_warnings)
            _finish_target_run(
                status,
                cycles,
                stop_reason="timeout" if timed_out else "idle",
                json_output=json_output,
                capture_warnings=capture_warnings,
            )
        collaboration_service(store).wait(
            store.get_runtime_revision(), timeout=min(max(interval, 0.05), 0.5)
        )


def _claim_for_target_pool_host(
    store, pool_host, candidate_publishers, *, target_task_id: str
):
    """Claim only the selected task tree while preserving host routing."""
    return _claim_cycle_slot(
        store,
        pool_host.host,
        candidate_publishers,
        target_contract_id=target_task_id,
        required_contract_capability=pool_host.required_contract_capability,
        request_prefix="run-target-claim",
    )


def _finish_target_run(
    status,
    cycles,
    *,
    stop_reason: str,
    json_output: bool,
    capture_warnings: list[str] | None = None,
) -> None:
    """Emit a target's actual projection plus a concise operator next step."""
    status["ok"] = status["status"] == "completed"
    status["stop_reason"] = stop_reason
    status["next_step"] = _target_next_step(status, stop_reason)
    status["cycles"] = cycles
    status["capture_warnings"] = list(capture_warnings or ())
    _emit_run_result(status, json_output)
    if not status["ok"]:
        raise click.exceptions.Exit(1)


def _target_next_step(status: dict, stop_reason: str) -> str:
    if stop_reason == "worker_failure":
        return (
            "Inspect aig task audit for the recorded failure category and recovery; "
            "run a recovery successor explicitly if one was published."
        )
    if stop_reason == "timeout":
        return (
            "Inspect aig task status and aig task audit; rerun after resolving "
            "the reported blockers."
        )
    if stop_reason == "terminal":
        if status["status"] == "completed":
            return (
                "Verify the committed outputs with aig task status and aig task audit."
            )
        return (
            "Inspect aig task audit; run a recovery successor explicitly if one "
            "was published."
        )
    task_status = status.get("status")
    if task_status in {"blocked", "blocked_delegation"}:
        return (
            "Resolve the listed input, activation, or delegation blockers, then "
            "rerun this task."
        )
    if task_status == "blocked_capability":
        return "Register a Worker with the required capability, then rerun this task."
    if task_status == "blocked_assignment":
        assignment = status.get("assignment") or {}
        blocker = assignment.get("blocker") or {}
        reason = blocker.get("reason", "assignment unavailable")
        worker_id = blocker.get("worker_id") or assignment.get("worker_id")
        if reason == "worker_at_capacity":
            cause = f"assigned Worker {worker_id} is at capacity"
        elif reason == "disabled_worker":
            cause = f"assigned Worker {worker_id} is disabled"
        elif reason == "missing_registration":
            cause = f"assigned Worker {worker_id} has no active registration"
        else:
            cause = f"assigned Worker {worker_id} is unavailable ({reason})"
        return (
            f"Resolve the assignment blocker: {cause}. Restore that Worker’s "
            "registration or capacity; another Worker cannot take over this "
            "assignment. Use `aig task assignments <CONTRACT_ID>` to copy the "
            "head. Only if this Contract has never been claimed can you use "
            "`aig task reassign <CONTRACT_ID> --worker <WORKER_ID> --previous "
            "<HEAD> --reason <TEXT>`."
        )
    if task_status == "claimed":
        return "Wait for the active claim to finish, then inspect status and rerun if work remains."
    if task_status == "submitted":
        if "qualification:independent_acceptance" in status.get("blockers", ()):
            return (
                "The signed Worker claim is already submitted and the exact declared "
                "business outputs await independent qualification. Inspect `aig task "
                "reviews <CONTRACT_ID>` and task audit for an existing decision. If "
                "that decision requests changes, publish its explicit successor with "
                "`aig task request-changes <DECISION_ID> --reason <TEXT>`; request a "
                "review only when no exact review exists. Do not rerun or reset this claim."
            )
        return (
            "The Worker claim is already submitted. Inspect task audit for incomplete "
            "or malformed outputs and any explicitly published recovery; do not reset "
            "or reuse this claim."
        )
    if task_status in {"stalled", "failed", "cancelled", "unreachable"}:
        return (
            "Inspect aig task audit; run a recovery successor explicitly if one "
            "was published."
        )
    return (
        "Inspect aig task status and aig task audit, then rerun when the task "
        "can make progress."
    )


def _worker_failure_categories(store, contract_ids: set[str]) -> list[str]:
    return sorted(
        {
            str(record.payload.get("category", "unknown"))
            for _, record in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
            if record.payload.get("contract_id") in contract_ids
        }
    )


def _cycle_trace_scope(window_records) -> tuple[list[str], list[str], int]:
    """Resolve cycle trace ownership from the complete bounded record window."""
    records = [record for _, record in window_records]
    records_by_id = {record.id: record for record in records}
    trace_records = [
        record for record in records if record.record_type == "trace.recorded"
    ]
    contracts: set[str] = set()
    unassigned_trace_ids: list[str] = []
    for record in trace_records:
        references = runtime_record_contract_references(record, records_by_id)
        contracts.update(references)
        if references:
            continue
        trace_payload = record.payload.get("trace")
        if isinstance(trace_payload, Mapping):
            trace_id = trace_payload.get("id")
            if isinstance(trace_id, str):
                unassigned_trace_ids.append(trace_id)
    return sorted(contracts), sorted(unassigned_trace_ids), len(trace_records)


def _emit_run_result(payload: dict, json_output: bool) -> None:
    if json_output:
        _output_json(payload)
        return
    status = payload.get("status", "unknown")
    click.echo(f"run {status}")
    if payload.get("contract_id"):
        click.echo(f"contract_id: {payload['contract_id']}")
    if payload.get("stop_reason"):
        click.echo(f"stop_reason: {payload['stop_reason']}")
    if payload.get("blockers"):
        click.echo(f"blockers: {', '.join(payload['blockers'])}")
    if payload.get("rejections"):
        click.echo(f"rejections: {', '.join(payload['rejections'])}")
    if payload.get("recoveries"):
        click.echo(f"recoveries: {', '.join(payload['recoveries'])}")
    if payload.get("failure_categories"):
        click.echo(f"failure_categories: {', '.join(payload['failure_categories'])}")
    for detail in payload.get("worker_failure_details", ()):
        click.echo(format_worker_failure_detail(detail))
    if payload.get("next_step"):
        click.echo(f"next_step: {payload['next_step']}")


@click.command("demo")
@click.argument("goal")
@click.option(
    "--worker",
    "worker_kind",
    type=click.Choice(["mock", "llm"]),
    default="llm",
    show_default=True,
    help="Worker implementation; mock is only for explicit tests and dry runs.",
)
@click.option(
    "--model",
    default=None,
    envvar="AIGINEERING_MODEL",
    help="LLM model name (or AIGINEERING_MODEL).",
)
@click.option(
    "--base-url",
    default="https://api.openai.com/v1",
    envvar="AIGINEERING_BASE_URL",
    show_default=True,
    help="OpenAI-compatible base URL when --worker llm.",
)
@click.option(
    "--timeout",
    type=float,
    default=60.0,
    show_default=True,
    help="Request timeout in seconds.",
)
@click.option(
    "--max-retries",
    type=int,
    default=3,
    show_default=True,
    help="Maximum retries for transient errors.",
)
@click.option(
    "--capability",
    "capabilities_str",
    default=None,
    help="Comma-separated provider capabilities (e.g. tool_calling,json_schema).",
)
@click.option(
    "--behavior-label",
    "behavior_labels",
    multiple=True,
    default=(),
    help="Behavior label to inject (repeatable).",
)
@click.option(
    "--save-config",
    "save_config",
    is_flag=True,
    default=False,
    help="Persist a sealed provider config snapshot before running.",
)
def demo(
    goal: str,
    worker_kind: str,
    model: Optional[str],
    base_url: str,
    timeout: float,
    max_retries: int,
    capabilities_str: Optional[str],
    behavior_labels: tuple[str, ...],
    save_config: bool,
) -> None:
    """Run a quick goal through a real LLM Worker by default."""
    capabilities = _parse_capabilities(capabilities_str)
    try:
        store, trace_store, contract, _scope = _run_demo(
            goal,
            worker_kind=worker_kind,
            model=model,
            base_url=base_url,
            save_config=save_config,
            timeout=timeout,
            max_retries=max_retries,
            capabilities=capabilities,
            behavior_labels=behavior_labels,
            capture_db_path=".aig/store.db",
        )
    except QueryProjectionUnavailable:
        raise
    except (RuntimeError, WorkerInvocationError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Demo completed for goal: '{goal}'")
    for warning in _scope.capture_warnings:
        click.echo(f"capture warning: {warning}")
    click.echo(f"  Contract: {contract.name}")
    click.echo(f"  Assets: {[a.name for a in store.get_all_assets()]}")
