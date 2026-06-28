"""Agent-facing task commands."""

from __future__ import annotations

import json
import time
from pathlib import Path

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.cli.task_state import project_task_status
from aigineering.core.control_plane import inject_contract
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.protocol.wire import trace_entry_to_dict


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
@click.option("--activation", default="", help="Activation expression.")
@click.option("--budget", type=int, default=5, show_default=True, help="Task budget.")
@click.option("--label", "labels", multiple=True, help="Injection labels.")
@click.option("--tool", "tool_scope", multiple=True, help="Allowed tool scope.")
@click.option(
    "--sensitive-input-policy",
    default=None,
    help="Sensitive input policy as a JSON object.",
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
    sensitive_input_policy: str | None,
    as_json: bool,
) -> None:
    """Create a task through the unified runtime ingress."""
    if description_file is not None:
        description = Path(description_file).read_text()
    policy = None
    if sensitive_input_policy:
        try:
            policy = json.loads(sensitive_input_policy)
        except json.JSONDecodeError as e:
            raise click.UsageError(f"--sensitive-input-policy is not valid JSON: {e}")

    store = _persistent_store()
    ingress = RuntimeIngress(store, store)
    try:
        contract = inject_contract(
            store,
            store,
            name=name,
            description=description,
            inputs=inputs,
            outputs=outputs,
            activation=activation,
            budget=budget,
            labels=labels,
            tool_scope=tool_scope,
            sensitive_input_policy=policy,
            ingress=ingress,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

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
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        _emit_error(f"Task '{contract_id}' not found.", as_json)
        return
    status = project_task_status(contract, store)
    if as_json:
        _output_json(status)
        return
    click.echo(f"{status['contract_id']} {status['status']}")
    if status["outputs"]:
        for name, asset_id in status["outputs"].items():
            click.echo(f"  output {name}: {asset_id}")
    if status["rejection_count"]:
        click.echo(f"  rejections: {status['rejection_count']}")
    if status["recovery_count"]:
        click.echo(f"  recoveries: {status['recovery_count']}")


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
    deadline = time.monotonic() + timeout_seconds
    last_status: dict | None = None
    while True:
        store = _persistent_store()
        contract = store.get_contract(contract_id)
        if contract is None:
            _emit_error(f"Task '{contract_id}' not found.", as_json)
            return
        last_status = project_task_status(contract, store)
        if last_status["terminal"]:
            if as_json:
                _output_json(last_status)
            else:
                click.echo(f"{contract_id} {last_status['status']}")
            return
        if time.monotonic() >= deadline:
            payload = dict(last_status)
            payload["timed_out"] = True
            if as_json:
                _output_json(payload)
            else:
                click.echo(f"{contract_id} {last_status['status']} (timed out)")
            return
        time.sleep(max(interval, 0.05))


@task_group.command("audit")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def task_audit(contract_id: str, as_json: bool) -> None:
    """Show an audit projection for a task."""
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        _emit_error(f"Task '{contract_id}' not found.", as_json)
        return
    entries = getattr(store, "get_by_contract", lambda _cid: [])(contract.id)
    status = project_task_status(contract, store)
    payload = {
        "task": status,
        "inputs": list(contract.inputs),
        "outputs_declared": list(contract.outputs),
        "labels": list(contract.labels),
        "tool_scope": list(contract.tool_scope),
        "trace": [trace_entry_to_dict(entry) for entry in entries],
    }
    if as_json:
        _output_json(payload)
        return
    click.echo(f"{contract.id} {status['status']}")
    click.echo(f"inputs: {list(contract.inputs)}")
    click.echo(f"outputs: {status['outputs']}")
    click.echo(f"trace_events: {len(entries)}")


def _emit_error(message: str, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": message})
    else:
        raise click.ClickException(message)
