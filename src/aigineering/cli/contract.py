"""CLI commands for contract injection and inspection (control-plane ingress)."""

from __future__ import annotations

import json

import click

from aigineering.cli._common import (
    _output_json,
    _persistent_snapshot,
    _persistent_store,
    _query_projection,
)
from aigineering.core.domain import load_genesis
from aigineering.task_publication import prepare_task_contract, publish_task_contract


@click.group("contract")
def contract_group() -> None:
    """Inject and inspect contracts through the control plane."""
    pass


@contract_group.command("add")
@click.option("--name", required=True, help="Contract name.")
@click.option(
    "--input", "inputs", multiple=True, help="Input asset names (repeatable)."
)
@click.option(
    "--output", "outputs", multiple=True, help="Output asset names (repeatable)."
)
@click.option("--activation", default="", help="Activation expression.")
@click.option("--budget", type=int, default=5, help="Budget (default 5).")
@click.option("--label", "labels", multiple=True, help="Labels (repeatable).")
@click.option("--tool", "tool_scope", multiple=True, help="Tool scope (repeatable).")
@click.option("--requires-capability", "worker_capabilities", multiple=True)
@click.option("--worker-pool", "worker_pools", multiple=True)
@click.option("--delegate-capability", "delegation_capabilities", multiple=True)
@click.option("--delegate-pool", "delegation_pools", multiple=True)
@click.option(
    "--sensitive-input-policy",
    "sensitive_input_policy",
    default=None,
    help='Sensitive input policy as JSON string (e.g. \'{"required_trust_tier":"verified"}\').',
)
@click.option(
    "--acceptance-policy",
    default=None,
    help='Output acceptance policy as JSON, for example {"mode":"independent","policy_version":"review-v1","verifier_capabilities":["verify.human"]}.',
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def contract_add(
    name: str,
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
    """Inject a contract into the runtime store."""
    policy = _parse_json_object(sensitive_input_policy, "--sensitive-input-policy")
    output_policy = _parse_json_object(acceptance_policy, "--acceptance-policy")
    try:
        contract = prepare_task_contract(
            {
                "name": name,
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

    if as_json:
        _output_json({"id": contract.id, "name": contract.name})
    else:
        click.echo(f"Contract injected: {contract.name} ({contract.id})")


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


@contract_group.command("ls")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def contract_list(as_json: bool) -> None:
    """List contracts in the store."""
    store = _contract_snapshot()
    try:
        all_contracts = _query_projection(store).get_all_contracts()
    finally:
        store.close()
    if as_json:
        _output_json([{"id": c.id, "name": c.name} for c in all_contracts])
    else:
        if not all_contracts:
            click.echo("No contracts found.")
        for c in all_contracts:
            click.echo(f"{c.id[:30]:<32} {c.name}")


@contract_group.command("show")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def contract_show(contract_id: str, as_json: bool) -> None:
    """Show contract details by ID."""
    store = _contract_snapshot()
    try:
        contract = _query_projection(store).get_contract(contract_id)
    finally:
        store.close()
    if contract is None:
        raise click.ClickException(f"No contract with id '{contract_id}'")

    if as_json:
        _output_json(
            {
                "id": contract.id,
                "name": contract.name,
                "inputs": list(contract.inputs),
                "outputs": list(contract.outputs),
                "activation": contract.activation,
                "budget": contract.budget,
                "acceptance_policy": contract.acceptance_policy,
                "labels": list(contract.labels),
                "tool_scope": list(contract.tool_scope),
            }
        )
    else:
        click.echo(f"id:         {contract.id}")
        click.echo(f"name:       {contract.name}")
        click.echo(f"inputs:     {list(contract.inputs)}")
        click.echo(f"outputs:    {list(contract.outputs)}")
        click.echo(f"activation: {contract.activation}")
        click.echo(f"budget:     {contract.budget}")
        click.echo(f"acceptance: {contract.acceptance_policy}")
        click.echo(f"labels:     {list(contract.labels)}")


@contract_group.command("run")
@click.argument("contract_id")
def contract_run(contract_id: str) -> None:
    """Deprecated direct execution entry."""
    raise click.ClickException(
        "aig contract run is deprecated. Use 'aig run --task "
        f"{contract_id} --worker <mock|llm>' or 'aig run --once'."
    )


def _contract_snapshot():
    try:
        store = _persistent_snapshot()
    except FileNotFoundError as error:
        raise click.ClickException(
            "Local domain is not initialized; run 'aig domain init'."
        ) from error
    try:
        load_genesis(store)
    except LookupError as error:
        store.close()
        raise click.ClickException(
            "Local domain is not initialized; run 'aig domain init'."
        ) from error
    except Exception:
        store.close()
        raise
    return store
