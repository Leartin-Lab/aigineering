"""aig run and aig demo commands."""

from __future__ import annotations

import time
from typing import Optional

import click

from aigineering.cli._common import (
    _asset_names_for,
    _get_trace_dir,
    _output_json,
    _persistent_store,
    _run_demo,
    _session_id,
)
from aigineering.cli.task_state import project_task_status
from aigineering.cli.worker_runtime import (
    build_worker,
    claim_next_package,
    execute_claimed_package,
)
from aigineering.core.session import SessionStore
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import Session, TraceEntry


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
    entries: list[TraceEntry],
) -> None:
    status = (
        "complete"
        if entries and any(e.event_type == "complete" for e in entries)
        else "incomplete"
    )
    _output_json(
        {
            "contract_id": contract_id,
            "session_id": session_id,
            "trace_ids": trace_ids,
            "status": status,
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
    "--worker-id",
    default=None,
    help="Worker identity for claim ownership. Defaults to the worker implementation id.",
)
@click.option(
    "--lease-seconds",
    type=int,
    default=60,
    show_default=True,
    help="Claim lease duration for task-pool execution.",
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
    worker_id: Optional[str],
    lease_seconds: int,
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
    """Run a CLI worker cycle or execute the legacy quick contract demo."""
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
            worker_id=worker_id,
            lease_seconds=lease_seconds,
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

    if json_output:
        _output_run_json(
            contract_id=contract.id,
            trace_ids=trace_ids,
            session_id=session_id,
            entries=entries,
        )
        return

    if not entries:
        click.echo("No trace entries recorded.")
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


def _run_task_pool(
    *,
    target_task_id: str | None,
    worker_kind: str,
    model: str | None,
    base_url: str,
    timeout: float,
    max_retries: int,
    capabilities: frozenset[str] | None,
    worker_id: str | None,
    lease_seconds: int,
    wait_timeout: float,
    interval: float,
    json_output: bool,
) -> None:
    store = _persistent_store()
    try:
        worker = build_worker(
            worker_kind,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            capabilities=capabilities,
        )
    except ValueError as e:
        raise click.ClickException(str(e))
    claim_worker_id = worker_id or getattr(worker, "worker_id", "cli-worker")
    deadline = time.monotonic() + wait_timeout
    cycles: list[dict] = []

    while True:
        if target_task_id:
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
                return
            status = project_task_status(target, store)
            if status["terminal"]:
                status["ok"] = status["status"] == "completed"
                status["cycles"] = cycles
                _emit_run_result(status, json_output)
                return

        claimed = claim_next_package(
            store,
            worker_id=claim_worker_id,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            if not target_task_id:
                _emit_run_result(
                    {
                        "ok": True,
                        "status": "idle",
                        "cycles": cycles,
                    },
                    json_output,
                )
                return
            if time.monotonic() >= deadline:
                status = project_task_status(target, store)
                status["ok"] = False
                status["timed_out"] = True
                status["cycles"] = cycles
                _emit_run_result(status, json_output)
                return
            time.sleep(max(interval, 0.05))
            store = _persistent_store()
            continue

        result = execute_claimed_package(claimed, worker, store)
        cycles.append(
            {
                "contract_id": claimed.contract.id,
                "contract_name": claimed.contract.name,
                "status": result.get("status"),
                "accepted_count": len(result.get("accepted_assets", [])),
                "rejected_count": len(result.get("rejected_candidates", [])),
            }
        )
        if not target_task_id:
            result = dict(result)
            result["ok"] = result.get("status") in {"accepted", "partial"}
            result["cycles"] = cycles
            _emit_run_result(result, json_output)
            return
        store = _persistent_store()


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
    click.echo(f"Demo completed for goal: '{goal}'")
    click.echo(f"  Contract: {contract.name}")
    click.echo(f"  Assets: {[a.name for a in store.get_all_assets()]}")
