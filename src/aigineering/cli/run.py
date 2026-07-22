"""aig run and aig demo commands."""

from __future__ import annotations

import json
import time
from typing import Optional

import click

from aigineering.cli._common import (
    _asset_names_for,
    _default_completion_registry,
    _get_trace_dir,
    _output_json,
    _persistent_store,
    _run_demo,
    _session_id,
)
from aigineering.cli.task_state import project_task_status
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
    process_rejected_submissions,
)
from aigineering.core.session import SessionStore
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import Session


def _parse_capabilities(capabilities_str: Optional[str]) -> frozenset[str] | None:
    """Parse a comma-separated capabilities string into a frozenset."""
    if capabilities_str is None:
        return None
    parts = [c.strip() for c in capabilities_str.split(",") if c.strip()]
    return frozenset(parts) if parts else None


def _output_run_json(
    contract_id: str,
    trace_ids: list[str],
    session_id: str,
    task_status: dict,
) -> None:
    _output_json(
        {
            "contract_id": contract_id,
            "session_id": session_id,
            "trace_ids": trace_ids,
            "status": "complete" if task_status["ok"] else task_status["status"],
            "ok": task_status["ok"],
            "blockers": task_status["blockers"],
        }
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
    default=None,
    help="Worker implementation to use (required for non-demo runs).",
)
@click.option("--model", default=None, help="LLM model name when --worker llm.")
@click.option(
    "--base-url",
    default="https://api.openai.com/v1",
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
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def run(
    goal: Optional[str],
    run_once: bool,
    target_task_id: Optional[str],
    wait_timeout: float,
    interval: float,
    mock_output: Optional[str],
    mock_presets: tuple[str, ...],
    worker_kind: Optional[str],
    model: Optional[str],
    base_url: str,
    timeout: float,
    max_retries: int,
    capabilities_str: Optional[str],
    behavior_labels: tuple[str, ...],
    save_config: bool,
    json_output: bool,
) -> None:
    """Run one Worker cycle, a target task, or a quick goal task graph."""
    if worker_kind is None:
        raise click.UsageError(
            "--worker is required.  Use 'mock' for deterministic testing, "
            "'llm' for OpenAI-compatible models, or 'aig demo' for the "
            "quickstart experience."
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
            capabilities=capabilities,
            mock_output=mock_output,
            mock_presets=mock_presets,
            wait_timeout=wait_timeout,
            interval=interval,
            json_output=json_output,
        )
        return

    if goal is None:
        raise click.UsageError("Provide GOAL, --once, or --task <contract_id>.")

    session_id = _session_id()
    trace_path = _get_trace_dir() / f"{session_id}.jsonl"
    jsonl_store = JsonLTraceStore(str(trace_path))
    try:
        store, trace_store, contract = _run_demo(
            goal,
            trace_store=jsonl_store,
            store=_persistent_store(),
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
    entries = trace_store.get_by_contract(contract.id)
    trace_ids = [e.id for e in jsonl_store.get_all()]

    # ── Session manifest ───────────────────────────────────────────────────
    contract_ids = [c.id for c in store.get_all_contracts()]
    asset_ids = [a.id for a in store.get_all_assets()]
    session = Session(
        id=session_id,
        root_contract_id=contract.id,
        contract_ids=contract_ids,
        asset_ids=asset_ids,
        trace_ids=trace_ids,
    )
    session_store = SessionStore()
    session_store.create_session(session)
    task_status = project_task_status(contract, store)

    if json_output:
        _output_run_json(
            contract_id=contract.id,
            trace_ids=trace_ids,
            session_id=session_id,
            task_status=task_status,
        )
        if not task_status["ok"]:
            raise click.exceptions.Exit(1)
        return

    if not entries:
        click.echo("No trace entries recorded.")
        raise click.exceptions.Exit(1)

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
        click.echo(f"Run ended without satisfied outputs: {task_status['status']}")
        raise click.exceptions.Exit(1)


def _run_task_pool(
    *,
    target_task_id: str | None,
    worker_kind: str,
    model: str | None,
    base_url: str,
    timeout: float,
    max_retries: int,
    capabilities: frozenset[str] | None,
    mock_output: str | None,
    mock_presets: tuple[str, ...],
    wait_timeout: float,
    interval: float,
    json_output: bool,
) -> None:
    store = _persistent_store()
    cycles: list[dict] = []
    try:
        worker = build_worker(
            worker_kind,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            capabilities=capabilities,
        )
        _configure_pool_worker(
            worker,
            store,
            worker_kind=worker_kind,
            mock_output=mock_output,
            mock_presets=mock_presets,
        )
        host = ensure_local_worker_host(store, worker)
        deadline = time.monotonic() + wait_timeout
        candidate_publishers = ensure_local_runtime_publishers(store)
        registry = _default_completion_registry()
        if target_task_id is None:
            _run_single_pool_cycle(
                store,
                host,
                candidate_publishers,
                registry,
                json_output=json_output,
            )
            return

        _run_target_pool(
            store,
            host,
            candidate_publishers,
            registry,
            target_task_id=target_task_id,
            deadline=deadline,
            interval=interval,
            cycles=cycles,
            json_output=json_output,
        )
    except WorkerInvocationError as e:
        _emit_run_result(
            {
                "ok": False,
                "status": "failed",
                "error": str(e),
                "cycles": cycles,
            },
            json_output,
        )
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


def _run_single_pool_cycle(
    store, host, candidate_publishers, registry, *, json_output: bool
) -> None:
    recovered = process_rejected_submissions(
        store, candidate_publishers=candidate_publishers
    )
    processed_before = process_task_completions(
        store, registry, candidate_publishers=candidate_publishers
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        candidate_publishers=candidate_publishers,
    )
    if claimed is None:
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
    result = execute_claimed_package(
        claimed,
        host,
        store,
        candidate_publishers=candidate_publishers,
    )
    processed_after = process_task_completions(
        store, registry, candidate_publishers=candidate_publishers
    )
    status = project_task_status(claimed.contract, store)
    status["ok"] = status["status"] == "completed"
    status["submission_status"] = result["status"]
    status["cycles"] = [
        {
            "contracts": [claimed.contract.id],
            "trace_events": len(store.get_by_contract(claimed.contract.id)),
            "tasks_processed": processed_before + processed_after,
            "rejections_recovered": recovered,
        }
    ]
    _emit_run_result(status, json_output)
    if not status["ok"]:
        raise click.exceptions.Exit(1)


def _run_target_pool(
    store,
    host,
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
        before_trace_count = len(store.get_all())
        recovered = process_rejected_submissions(
            store, candidate_publishers=candidate_publishers
        )
        processed_before = process_task_completions(
            store, registry, candidate_publishers=candidate_publishers
        )
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            candidate_publishers=candidate_publishers,
        )
        submission = (
            execute_claimed_package(
                claimed,
                host,
                store,
                candidate_publishers=candidate_publishers,
            )
            if claimed is not None
            else None
        )
        processed_after = process_task_completions(
            store, registry, candidate_publishers=candidate_publishers
        )
        new_entries = store.get_all()[before_trace_count:]
        touched_contracts = sorted({entry.contract_id for entry in new_entries})
        if touched_contracts:
            cycles.append(
                {
                    "contracts": touched_contracts,
                    "trace_events": len(new_entries),
                    "submission_status": (
                        submission.get("status") if submission is not None else None
                    ),
                    "tasks_processed": processed_before + processed_after,
                    "rejections_recovered": recovered,
                }
            )

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
            status["ok"] = status["status"] == "completed"
            status["cycles"] = cycles
            _emit_run_result(status, json_output)
            if not status["ok"]:
                raise click.exceptions.Exit(1)
            return

        timed_out = time.monotonic() >= deadline
        idle = (
            claimed is None
            and not processed_before
            and not processed_after
            and not recovered
        )
        if timed_out or idle:
            status["ok"] = status["status"] == "completed"
            if status.get("silent_failure_risks"):
                status["status"] = "stalled"
            status["timed_out"] = timed_out
            status["cycles"] = cycles
            _emit_run_result(status, json_output)
            if not status["ok"]:
                raise click.exceptions.Exit(1)
            return
        time.sleep(max(interval, 0.05))


def _emit_run_result(payload: dict, json_output: bool) -> None:
    if json_output:
        _output_json(payload)
        return
    status = payload.get("status", "unknown")
    click.echo(f"run {status}")
    if payload.get("contract_id"):
        click.echo(f"contract_id: {payload['contract_id']}")


@click.command("demo")
@click.argument("goal")
@click.option(
    "--worker",
    "worker_kind",
    type=click.Choice(["mock", "llm"]),
    default="mock",
    show_default=True,
    help="Worker implementation to use.",
)
@click.option("--model", default=None, help="LLM model name when --worker llm.")
@click.option(
    "--base-url",
    default="https://api.openai.com/v1",
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
    """Run a quick demo with the given goal (quickstart experience)."""
    capabilities = _parse_capabilities(capabilities_str)
    try:
        store, trace_store, contract = _run_demo(
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
