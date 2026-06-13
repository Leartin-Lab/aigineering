"""aig retry command."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.core.ids import hash_retry
from aigineering.core.method_handlers.retry import RetryMethodHandler
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.trace import MemoryTraceStore
from aigineering.protocol.types import Candidate


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

    # Method ingress (G1): dispatch through RetryMethodHandler instead of
    # directly calling store.add_contract().
    runtime = MethodRuntime(
        store=store,
        trace=MemoryTraceStore(),
        budget={},
    )
    candidate = Candidate(
        worker_id="cli",
        raw_output="/retry",
        parsed_action={"type": "retry"},
    )
    handler = RetryMethodHandler()
    handler.handle_method(runtime, original, "retry", candidate)

    retry_id = hash_retry(contract_id)

    if json_output:
        _output_json({
            "original_contract_id": contract_id,
            "retry_contract_id": retry_id,
        })
    else:
        click.echo(f"Retry contract created: {retry_id}")
        click.echo(f"  Original: {contract_id}")
