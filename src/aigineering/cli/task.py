"""Agent-facing task commands."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from collections.abc import Mapping

import click

from aigineering.application import collaboration_service
from aigineering.cli._common import (
    _output_json,
    _persistent_snapshot,
    _persistent_store,
)
from aigineering.cli.task_state import project_task_status
from aigineering.core.session import runtime_record_contract_references
from aigineering.core.task_productivity import project_task_productivity
from aigineering.protocol.wire import trace_entry_to_dict
from aigineering.task_publication import prepare_task_contract, publish_task_contract
from aigineering.cli.task_priority import register_task_priority_commands
from aigineering.cli.task_diagnostics import register_task_diagnostic_commands
from aigineering.cli.task_context import register_task_context_command
from aigineering.cli.task_logs import register_task_log_command
from aigineering.cli.task_attachments import register_task_attachment_command
from aigineering.cli.task_lineage import register_task_lineage_command


@click.group("task")
def task_group() -> None:
    """Create, inspect, wait for, and audit tasks."""


@task_group.command("create")
@click.option("--name", required=True, help="Task name.")
@click.option("--description", default="", help="Task description.")
@click.option(
    "--description-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Read task description from a file.",
)
@click.option("--input", "inputs", multiple=True, help="Input asset names.")
@click.option("--output", "outputs", multiple=True, help="Declared output names.")
@click.option(
    "--activation",
    default="",
    help="Asset-name expression using uppercase AND/OR and parentheses; no NOT.",
)
@click.option(
    "--budget",
    type=int,
    default=5,
    show_default=True,
    help="Causal task allowance for work; not a token or currency limit.",
)
@click.option(
    "--label",
    "labels",
    multiple=True,
    help="Select frozen label-to-Asset context; grants no capability or authority.",
)
@click.option("--tool", "tool_scope", multiple=True, help="Allowed tool scope.")
@click.option(
    "--requires-capability",
    "worker_capabilities",
    multiple=True,
    help="Worker capability required to execute this task.",
)
@click.option(
    "--worker-pool", "worker_pools", multiple=True, help="Eligible worker pool."
)
@click.option(
    "--delegate-capability",
    "delegation_capabilities",
    multiple=True,
    help="Capability descendants may require.",
)
@click.option(
    "--delegate-pool",
    "delegation_pools",
    multiple=True,
    help="Worker pool descendants may select.",
)
@click.option(
    "--sensitive-input-policy",
    default=None,
    help="Sensitive input policy as a JSON object.",
)
@click.option(
    "--acceptance-policy",
    default=None,
    help="Output acceptance policy as a JSON object.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def task_create(
    name: str,
    description: str,
    description_file: str | None,
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
    activation: str,
    budget: int,
    labels: tuple[str, ...],
    tool_scope: tuple[str, ...],
    worker_capabilities: tuple[str, ...],
    worker_pools: tuple[str, ...],
    delegation_capabilities: tuple[str, ...],
    delegation_pools: tuple[str, ...],
    sensitive_input_policy: str | None,
    acceptance_policy: str | None,
    as_json: bool,
) -> None:
    """Create one ordinary task through authenticated Candidate commitment."""
    if budget < 1:
        raise click.BadParameter("must be a positive integer", param_hint="--budget")
    if description_file is not None:
        try:
            description = Path(description_file).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise click.UsageError(
                f"--description-file must be readable UTF-8 text: {description_file}"
            ) from error
    policy = _parse_json_object(sensitive_input_policy, "--sensitive-input-policy")
    output_policy = _parse_json_object(acceptance_policy, "--acceptance-policy")

    try:
        contract = prepare_task_contract(
            {
                "name": name,
                "description": description,
                "inputs": inputs,
                "outputs": outputs,
                "activation": activation,
                "budget": budget,
                "labels": labels,
                "tool_scope": tool_scope,
                "worker_capabilities": worker_capabilities,
                "worker_pools": worker_pools,
                "delegation_capabilities": delegation_capabilities,
                "delegation_pools": delegation_pools,
                "sensitive_input_policy": policy,
                "acceptance_policy": output_policy,
            }
        )
    except (LookupError, TypeError, ValueError) as e:
        raise click.ClickException(str(e)) from e

    store = _persistent_store()
    try:
        contract = publish_task_contract(store, contract)
    except (LookupError, TypeError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    finally:
        store.close()

    payload = {
        "ok": True,
        "contract_id": contract.id,
        "name": contract.name,
        "outputs": list(contract.outputs),
    }
    if as_json:
        _output_json(payload)
    else:
        click.echo(f"Task created: {contract.name} ({contract.id})")


@task_group.command("status")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def task_status(contract_id: str, as_json: bool) -> None:
    """Show a projected task status."""
    try:
        store = _persistent_snapshot()
        try:
            contract = store.get_contract(contract_id)
            if contract is None:
                _emit_error(f"Task '{contract_id}' not found.", as_json)
                return
            status = project_task_status(contract, store, check_integrity=True)
        finally:
            store.close()
    except (click.exceptions.Exit, click.ClickException):
        raise
    except Exception as error:
        _emit_inspection_error(error, as_json)
        return
    if as_json:
        _output_json(status)
        if not status["integrity"]["consistent"]:
            raise click.exceptions.Exit(1)
        return
    click.echo(f"{status['contract_id']} {status['status']}")
    if status["outputs"]:
        for name, asset_id in status["outputs"].items():
            click.echo(f"  output {name}: {asset_id}")
    if status["rejection_count"]:
        click.echo(f"  rejections: {status['rejection_count']}")
    if status["recovery_count"]:
        click.echo(f"  recoveries: {status['recovery_count']}")
    click.echo(f"  integrity: {status['integrity']['status']}")
    if not status["integrity"]["consistent"]:
        click.echo("  projected status is untrusted")
        raise click.exceptions.Exit(1)


@task_group.command("wait")
@click.argument("contract_id")
@click.option(
    "--timeout", "timeout_seconds", type=float, default=60.0, show_default=True
)
@click.option("--interval", type=float, default=1.0, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def task_wait(
    contract_id: str,
    timeout_seconds: float,
    interval: float,
    as_json: bool,
) -> None:
    """Poll until a task reaches a terminal projected status."""
    _validate_wait_durations(timeout_seconds, interval)
    deadline = time.monotonic() + timeout_seconds
    last_status: dict | None = None
    while True:
        try:
            store = _persistent_snapshot()
            try:
                contract = store.get_contract(contract_id)
                if contract is None:
                    _emit_error(f"Task '{contract_id}' not found.", as_json)
                    return
                collaboration_service(store).sync()
                last_status = project_task_status(contract, store)
                if last_status["terminal"]:
                    last_status = project_task_status(
                        contract, store, check_integrity=True
                    )
                    if as_json:
                        _output_json(last_status)
                    else:
                        click.echo(f"{contract_id} {last_status['status']}")
                        click.echo(f"integrity: {last_status['integrity']['status']}")
                    if not last_status["integrity"]["consistent"]:
                        raise click.exceptions.Exit(1)
                    if last_status["status"] != "completed":
                        raise click.exceptions.Exit(1)
                    return
                if time.monotonic() >= deadline:
                    payload = dict(last_status)
                    payload["timed_out"] = True
                    payload["integrity"] = {
                        "status": "not_checked",
                        "consistent": None,
                        "checked": [],
                        "not_checked": [
                            "terminal_local_runtime_record_and_materialization_check"
                        ],
                    }
                    payload["ok"] = False
                    if as_json:
                        _output_json(payload)
                    else:
                        click.echo(f"{contract_id} {last_status['status']} (timed out)")
                    raise click.exceptions.Exit(1)
            finally:
                store.close()
        except (click.exceptions.Exit, click.ClickException):
            raise
        except Exception as error:
            _emit_inspection_error(error, as_json)
            return
        time.sleep(min(max(interval, 0.05), 0.5))


@task_group.command("audit")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def task_audit(contract_id: str, as_json: bool) -> None:
    """Show an audit projection for a task."""
    try:
        store = _persistent_snapshot()
        try:
            contract = store.get_contract(contract_id)
            if contract is None:
                _emit_error(f"Task '{contract_id}' not found.", as_json)
                return
            entries = store.get_by_contract(contract.id)
            runtime_records = store.scan_runtime_records()
            record_index = {record.id: record for _, record in runtime_records}
            related_runtime_traces = []
            for revision, record in runtime_records:
                if record.record_type != "trace.recorded":
                    continue
                trace = record.payload.get("trace")
                if not isinstance(trace, Mapping):
                    continue
                if trace.get("contract_id") != "commitment":
                    continue
                if contract.id not in runtime_record_contract_references(
                    record, record_index
                ):
                    continue
                related_runtime_traces.append(
                    {
                        "trace_id": trace.get("id"),
                        "event_type": trace.get("event_type"),
                        "record_id": record.id,
                        "recorded_at": record.recorded_at,
                        "revision": revision,
                    }
                )
            status = project_task_status(contract, store, check_integrity=True)
            productivity = project_task_productivity(contract, store)
            payload = {
                "task": status,
                "inputs": list(contract.inputs),
                "outputs_declared": list(contract.outputs),
                "labels": list(contract.labels),
                "tool_scope": list(contract.tool_scope),
                "trace": [trace_entry_to_dict(entry) for entry in entries],
                "related_runtime_traces": related_runtime_traces,
                "productivity": productivity,
            }
        finally:
            store.close()
    except (click.exceptions.Exit, click.ClickException):
        raise
    except Exception as error:
        _emit_inspection_error(error, as_json)
        return
    if as_json:
        _output_json(payload)
        if not status["integrity"]["consistent"]:
            raise click.exceptions.Exit(1)
        return
    click.echo(f"{contract.id} {status['status']}")
    click.echo(f"inputs: {list(contract.inputs)}")
    click.echo(f"outputs: {status['outputs']}")
    click.echo(f"trace_events: {len(entries)}")
    click.echo(f"integrity: {status['integrity']['status']}")
    if not status["integrity"]["consistent"]:
        click.echo("projected status is untrusted")
        raise click.exceptions.Exit(1)


def _emit_error(message: str, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": message})
        raise click.exceptions.Exit(1)
    else:
        raise click.ClickException(message)


def _parse_json_object(value: str | None, option: str) -> dict | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise click.UsageError(f"{option} is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise click.UsageError(f"{option} must be a JSON object")
    return parsed


def _validate_wait_durations(timeout_seconds: float, interval: float) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise click.BadParameter("must be a finite number greater than or equal to 0")
    if not math.isfinite(interval) or interval <= 0:
        raise click.BadParameter("must be a finite number greater than 0")


def _emit_inspection_error(error: Exception, as_json: bool) -> None:
    """Report only an exception category; it may contain sealed row data."""
    payload = {
        "ok": False,
        "error": "Unable to inspect task state",
        "error_type": type(error).__name__,
    }
    if as_json:
        _output_json(payload)
    else:
        click.echo(f"{payload['error']} ({payload['error_type']})")
    raise click.exceptions.Exit(1)


register_task_priority_commands(task_group)
register_task_diagnostic_commands(task_group)
register_task_context_command(task_group)
register_task_log_command(task_group)
register_task_attachment_command(task_group)
register_task_lineage_command(task_group)
