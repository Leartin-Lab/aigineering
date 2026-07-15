"""aig retry command."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.cli._candidate import commit_local_effect, require_accepted
from aigineering.core.methods import retry_contract
from aigineering.protocol.effect_builders import contract_declaration_effect


@click.command("retry")
@click.option(
    "--contract", "contract_id", required=True, help="Original contract ID to retry."
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def retry(
    contract_id: str,
    json_output: bool,
) -> None:
    """Publish a security-equivalent retry Contract through a signed Candidate."""
    store = _persistent_store()
    original = store.get_contract(contract_id)
    if original is None:
        if json_output:
            _output_json({"error": f"Contract '{contract_id}' not found."})
        else:
            click.echo(f"Contract '{contract_id}' not found.")
        return

    proposed = retry_contract(original)
    try:
        require_accepted(
            commit_local_effect(
                store,
                contract_declaration_effect(proposed),
                idempotency_key=f"retry:{original.id}:{proposed.id}",
                causal_parents=(original.id,),
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    retry_id = proposed.id

    if json_output:
        _output_json(
            {
                "original_contract_id": contract_id,
                "retry_contract_id": retry_id,
            }
        )
    else:
        click.echo(f"Retry contract created: {retry_id}")
        click.echo(f"  Original: {contract_id}")
