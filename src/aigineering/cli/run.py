"""aig run and aig demo commands."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click

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
from aigineering.cli.task_state import (
    project_task_status,
    recovery_successor_map,
)
from aigineering.local_identity import (
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
)
from aigineering.application import build_worker
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_task_completions,
    run_runtime_maintenance_step,
)
from aigineering.core.session import SessionStore
from aigineering.core.session import runtime_record_contract_references
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import Session
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.cli._candidate import commit_local_effects, require_accepted


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
@click.argument("goal", required=False)
@click.option(
    "--once",
    "run_once",
    is_flag=True,
    default=False,
    help="Claim and execute one ready task from the local task pool.",
)
@click.option(
    "--task",
    "target_task_id",
    default=None,
    help="Run worker cycles until this task reaches a terminal status.",
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
def run(
    goal: Optional[str],
    run_once: bool,
    target_task_id: Optional[str],
    wait_timeout: float,
    interval: float,
    mock_output: Optional[str],
    mock_presets: tuple[str, ...],
    worker_kind: str,
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
    """Run one Worker cycle, a target task, or a quick goal task graph."""
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
            worker_kind=worker_kind,
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
) -> None:
    store = _persistent_store()
    cycles: list[dict] = []
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
            _run_single_pool_cycle(
                store,
                hosts,
                candidate_publishers,
                registry,
                json_output=json_output,
            )
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
        )
    except WorkerInvocationError as e:
        failure = {
            "ok": False,
            "status": "failed",
            "error": str(e),
            "cycles": cycles,
        }
        if target_task_id is not None:
            target = store.get_contract(target_task_id)
            if target is not None:
                failure.update(project_task_status(target, store))
                failure["ok"] = False
                failure["error"] = str(e)
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


def _publish_tool_descriptors(store, registry) -> None:
    descriptors = tuple(
        create_tool_descriptor(
            spec.name,
            spec.description,
            spec.input_schema,
            source_uri=f"python:{spec.name}",
            output_schema=spec.output_schema,
            version=spec.version,
            max_output_bytes=spec.max_output_bytes,
        )
        for spec in registry.list_specs()
    )
    effects = tuple(asset_proposal_effect(descriptor) for descriptor in descriptors)
    require_accepted(
        commit_local_effects(
            store,
            effects,
            idempotency_key="tool-registry:"
            + ",".join(sorted(descriptor.id for descriptor in descriptors)),
        )
    )


def _run_single_pool_cycle(
    store, hosts, candidate_publishers, registry, *, json_output: bool
) -> None:
    maintenance = run_runtime_maintenance_step(
        store, registry, candidate_publishers=candidate_publishers
    )
    recovered = list(maintenance.rejected_submissions)
    processed_before = list(maintenance.task_completions)
    claims = []
    submissions = []
    for pool_host in hosts:
        claimed = _claim_for_pool_host(
            store,
            pool_host,
            candidate_publishers,
        )
        if claimed is None:
            continue
        claims.append(claimed)
        submissions.append(
            execute_claimed_package(
                claimed,
                pool_host.host,
                store,
                candidate_publishers=candidate_publishers,
            )
        )
    if not claims:
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
    processed_after = process_task_completions(
        store, registry, candidate_publishers=candidate_publishers
    )
    status = project_task_status(claims[0].contract, store)
    status["ok"] = status["status"] == "completed"
    status["submission_status"] = submissions[0]["status"]
    status["cycles"] = [
        {
            "contracts": [claimed.contract.id for claimed in claims],
            "trace_events": sum(
                len(store.get_by_contract(claimed.contract.id)) for claimed in claims
            ),
            "tasks_processed": processed_before + processed_after,
            "rejections_recovered": recovered,
        }
    ]
    _emit_run_result(status, json_output)
    if not status["ok"]:
        raise click.exceptions.Exit(1)


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
) -> None:
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
        status = project_task_status(target, store)
        if status["terminal"]:
            _finish_target_run(
                status, cycles, stop_reason="terminal", json_output=json_output
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
            submissions.append(
                execute_claimed_package(
                    claimed,
                    pool_host.host,
                    store,
                    candidate_publishers=candidate_publishers,
                )
            )
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
        status = project_task_status(target, store)
        if status["terminal"]:
            _finish_target_run(
                status, cycles, stop_reason="terminal", json_output=json_output
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
            _finish_target_run(
                status,
                cycles,
                stop_reason="timeout" if timed_out else "idle",
                json_output=json_output,
            )
        time.sleep(max(interval, 0.05))


def _claim_for_target_pool_host(
    store, pool_host, candidate_publishers, *, target_task_id: str
):
    """Claim only the selected task tree while preserving host routing."""
    capability = pool_host.required_contract_capability
    from aigineering.runtime import claim_next_package

    contract_ids = _contract_tree_ids(store.get_all_contracts(), target_task_id)
    for contract_id in sorted(contract_ids):
        contract = store.get_contract(contract_id)
        if contract is None:
            continue
        if capability is not None and capability not in contract.worker_capabilities:
            continue
        claimed = claim_next_package(
            store,
            worker_id=pool_host.host.worker_id,
            contract_id=contract_id,
            candidate_publishers=candidate_publishers,
        )
        if claimed is not None:
            return claimed
    return None


def _finish_target_run(status, cycles, *, stop_reason: str, json_output: bool) -> None:
    """Emit a target's actual projection plus a concise operator next step."""
    status["ok"] = status["status"] == "completed"
    status["stop_reason"] = stop_reason
    status["next_step"] = _target_next_step(status, stop_reason)
    status["cycles"] = cycles
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
    if task_status == "claimed":
        return "Wait for the active claim to finish, then inspect status and rerun if work remains."
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


def _claim_for_pool_host(store, pool_host, candidate_publishers):
    capability = pool_host.required_contract_capability
    if capability is None:
        return claim_next_package(
            store,
            worker_id=pool_host.host.worker_id,
            candidate_publishers=candidate_publishers,
        )
    for contract in store.get_all_contracts():
        if capability not in contract.worker_capabilities:
            continue
        claimed = claim_next_package(
            store,
            worker_id=pool_host.host.worker_id,
            contract_id=contract.id,
            candidate_publishers=candidate_publishers,
        )
        if claimed is not None:
            return claimed
    return None


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
        )
    except (RuntimeError, WorkerInvocationError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Demo completed for goal: '{goal}'")
    click.echo(f"  Contract: {contract.name}")
    click.echo(f"  Assets: {[a.name for a in store.get_all_assets()]}")
