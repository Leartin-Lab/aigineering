"""Local heterogeneous Worker fleet commands."""

from __future__ import annotations

import json
import math

import click

from aigineering.cli._common import _output_json
from aigineering.configured_run import run_configured
from aigineering.run_config import prepare_configured_run
from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.fleet_service import (
    FleetServiceError,
    restart_service,
    start_service,
    status_service,
    stop_service,
)
from pathlib import Path


@click.group("fleet")
def fleet_group() -> None:
    """Run multiple capability-routed local Workers."""


@fleet_group.command("run")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="TOML Worker fleet configuration.",
)
@click.option("--task", "target_contract_id", required=True, help="Root task ID.")
@click.option(
    "--wait-timeout",
    type=float,
    default=300.0,
    show_default=True,
    help="Maximum seconds to run the bounded fleet.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def fleet_run(
    config_path: str,
    target_contract_id: str,
    wait_timeout: float,
    as_json: bool,
) -> None:
    """Run a local fleet through independent pull/claim/submit loops."""
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise click.BadParameter(
            "must be a finite positive number", param_hint="--wait-timeout"
        )
    try:
        prepared = prepare_configured_run(
            config_path, resume_task_id=target_contract_id
        )
        result = run_configured(prepared, timeout=wait_timeout)
    except QueryProjectionUnavailable:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    payload = {
        "contract_id": result.contract_id,
        "status": result.status,
        "completed": result.completed,
        "timed_out": result.timed_out,
        "worker_errors": list(result.worker_errors),
        "worker_failure_details": list(result.worker_failure_details),
        "capture_warnings": list(result.capture_warnings),
    }
    if as_json:
        _output_json(payload)
    else:
        click.echo(f"fleet {result.status}: {result.contract_id}")
        for error in result.worker_errors:
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


def _emit_service_result(result, *, as_json: bool) -> None:
    data = result.as_dict()
    if as_json:
        _output_json({"ok": True, "data": data})
        return
    click.echo(f"fleet service {result.state}")
    if result.generation:
        click.echo(f"generation: {result.generation}")
    if result.domain_id:
        click.echo(f"domain_id: {result.domain_id}")
    if result.active_slots:
        click.echo(f"active_slots: {result.active_slots}")
    for worker_id, version in zip(result.worker_ids, result.registration_versions):
        click.echo(f"worker: {worker_id} registration_version={version}")
    if result.error_code:
        click.echo(f"error_code: {result.error_code}")


def _service_error(exc: FleetServiceError, *, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": exc.code, "message": str(exc)})
        raise click.exceptions.Exit(1) from exc
    raise click.ClickException(str(exc)) from exc


@fleet_group.command("start")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="TOML Worker fleet configuration.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def fleet_start(config_path: str, as_json: bool) -> None:
    """Start one persistent local Fleet for the selected workspace."""
    try:
        result = start_service(Path.cwd(), config_path)
    except FleetServiceError as exc:
        _service_error(exc, as_json=as_json)
        return
    _emit_service_result(result, as_json=as_json)


@fleet_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def fleet_status(as_json: bool) -> None:
    """Inspect local Fleet process metadata without querying Redis."""
    try:
        result = status_service(Path.cwd())
    except FleetServiceError as exc:
        _service_error(exc, as_json=as_json)
        return
    _emit_service_result(result, as_json=as_json)


@fleet_group.command("stop")
@click.option(
    "--wait-timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Maximum seconds to wait for admitted pulls to drain.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def fleet_stop(wait_timeout: float, as_json: bool) -> None:
    """Close new pull admission and drain the selected local Fleet."""
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise click.BadParameter(
            "must be a finite positive number", param_hint="--wait-timeout"
        )
    try:
        result = stop_service(Path.cwd(), wait_timeout=wait_timeout)
    except FleetServiceError as exc:
        _service_error(exc, as_json=as_json)
        return
    _emit_service_result(result, as_json=as_json)
    if result.state == "stopping":
        raise click.exceptions.Exit(1)


@fleet_group.command("restart")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="TOML Worker fleet configuration.",
)
@click.option(
    "--wait-timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Maximum seconds to drain the previous generation.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def fleet_restart(config_path: str, wait_timeout: float, as_json: bool) -> None:
    """Drain the exact current generation, then start a newly validated one."""
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise click.BadParameter(
            "must be a finite positive number", param_hint="--wait-timeout"
        )
    try:
        result = restart_service(Path.cwd(), config_path, wait_timeout=wait_timeout)
    except FleetServiceError as exc:
        _service_error(exc, as_json=as_json)
        return
    _emit_service_result(result, as_json=as_json)


# Process inspection and stop requests are local controls and remain useful
# when required Redis is unavailable. Other Fleet operations retain the shared
# Redis preflight installed by cli.infrastructure.protect_commands.
fleet_status.callback._redis_protected = True
fleet_stop.callback._redis_protected = True
