"""CLI commands for contract injection and inspection (control-plane ingress)."""

from __future__ import annotations

import json

import click

from aigineering.cli._common import _persistent_store, _output_json
from aigineering.cli.domain import load_actor_signer
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import load_genesis
from aigineering.protocol.candidate import CandidateEffect, create_candidate_proposal
from aigineering.protocol.wire import contract_to_dict


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
@click.option(
    "--sensitive-input-policy",
    "sensitive_input_policy",
    default=None,
    help='Sensitive input policy as JSON string (e.g. \'{"required_trust_tier":"verified"}\').',
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
    sensitive_input_policy: str | None,
    as_json: bool,
) -> None:
    """Inject a contract into the runtime store."""
    store = _persistent_store()

    policy: dict | None = None
    if sensitive_input_policy:
        try:
            policy = json.loads(sensitive_input_policy)
        except json.JSONDecodeError as e:
            raise click.UsageError(f"--sensitive-input-policy is not valid JSON: {e}")
    try:
        genesis = load_genesis(store)
        signer = load_actor_signer()
        actor_key = next(
            key
            for key in genesis.root_keys
            if key.public_key == signer.signer_id and not key.revoked
        )
        contract = build_control_plane_contract(
            name=name,
            inputs=inputs,
            outputs=outputs,
            activation=activation,
            budget=budget,
            labels=labels,
            tool_scope=tool_scope,
            sensitive_input_policy=policy,
        )
        candidate = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=actor_key.actor_id,
            key_id=actor_key.key_id,
            effects=[
                CandidateEffect(
                    "contract.declare", {"contract": contract_to_dict(contract)}
                )
            ],
            signer=signer,
            idempotency_key=f"contract:{contract.id}",
        )
        decision = CandidateCommitter(store, store).commit(candidate)
        if not decision.accepted:
            rejection = next(
                record
                for record in decision.runtime_records
                if record.record_type.endswith("rejected")
            )
            raise ValueError(str(rejection.payload["reason"]))
    except (LookupError, StopIteration, ValueError) as e:
        if isinstance(e, StopIteration):
            message = "local actor key is not authorized by domain Genesis"
        else:
            message = str(e)
        raise click.ClickException(message) from e

    if as_json:
        _output_json({"id": contract.id, "name": contract.name})
    else:
        click.echo(f"Contract injected: {contract.name} ({contract.id})")


@contract_group.command("ls")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def contract_list(as_json: bool) -> None:
    """List contracts in the store."""
    store = _persistent_store()
    all_contracts = store.get_all_contracts()
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
    store = _persistent_store()
    contract = store.get_contract(contract_id)
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
        click.echo(f"labels:     {list(contract.labels)}")


@contract_group.command("run")
@click.argument("contract_id")
def contract_run(contract_id: str) -> None:
    """Deprecated direct execution entry."""
    raise click.ClickException(
        "aig contract run is deprecated. Use 'aig run --task "
        f"{contract_id} --worker <mock|llm>' or 'aig run --once'."
    )
