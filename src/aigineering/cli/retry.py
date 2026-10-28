"""aig retry command."""

from __future__ import annotations

import click

from aigineering.cli._common import _persistent_store
from aigineering.cli._retry_result import emit_retry_error, emit_retry_success
from aigineering.task_publication import retry_local_task_result


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
    try:
        data, revision = retry_local_task_result(store, contract_id)
    except Exception as exc:
        emit_retry_error(exc, as_json=json_output)
    finally:
        store.close()
    emit_retry_success(data, revision, as_json=json_output)
