"""aig retry command."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.core.ids import hash_retry
from aigineering.protocol.types import Contract


@click.command("retry")
@click.option("--contract", "contract_id", required=True, help="Original contract ID to retry.")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def retry(
    contract_id: str,
    json_output: bool,
) -> None:
    """Method-first retry: create a new contract from an existing one with deterministic retry ID."""
    store = _persistent_store()
    original = store.get_contract(contract_id)
    if original is None:
        if json_output:
            _output_json({"error": f"Contract '{contract_id}' not found."})
        else:
            click.echo(f"Contract '{contract_id}' not found.")
        return

    retry_id = hash_retry(contract_id)
    retry_contract = Contract(
        id=retry_id,
        parent_id=original.parent_id,
        name=original.name,
        description=original.description,
        inputs=original.inputs,
        outputs=original.outputs,
        activation=original.activation,
        budget=original.budget,
        tool_scope=original.tool_scope,
        labels=original.labels,
        origin=original.origin,
        sensitive_input_policy=original.sensitive_input_policy,
    )
    store.add_contract(retry_contract)

    if json_output:
        _output_json({
            "original_contract_id": contract_id,
            "retry_contract_id": retry_id,
        })
    else:
        click.echo(f"Retry contract created: {retry_id}")
        click.echo(f"  Original: {contract_id}")
