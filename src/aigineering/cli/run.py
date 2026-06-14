"""aig run and aig demo commands."""

from __future__ import annotations

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
from aigineering.core.session import SessionStore
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import Session, TraceEntry


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
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def run(
    goal: str,
    worker_kind: str,
    model: Optional[str],
    base_url: str,
    json_output: bool,
) -> None:
    """Execute a demo contract and persist the trace to JSONL."""
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
            worker_id = entry.worker_id or "mock_worker"
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
def demo(
    goal: str,
    worker_kind: str,
    model: Optional[str],
    base_url: str,
) -> None:
    """Run a quick demo with the given goal (quickstart experience)."""
    store, trace_store, contract = _run_demo(
        goal,
        worker_kind=worker_kind,
        model=model,
        base_url=base_url,
    )
    click.echo(f"Demo completed for goal: '{goal}'")
    click.echo(f"  Contract: {contract.name}")
    click.echo(f"  Assets: {[a.name for a in store.get_all_assets()]}")
