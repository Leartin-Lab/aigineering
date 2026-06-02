"""aig — Aigineering command-line interface."""

from __future__ import annotations

import json
from typing import Optional

import click

from aigineering.core.engine import Engine
from aigineering.core.ids import asset_id, contract_id
from aigineering.core.store import MemoryStore
from aigineering.core.trace import TraceStore
from aigineering.agent.mock import MockWorker
from aigineering.protocol.types import Asset, Contract, TraceEntry


def _asset_json(
    name: str,
    content: str,
    content_type: str = "text",
    created_by: str = "",
    origin: str = "human",
) -> str:
    return json.dumps(
        {"name": name, "content": content, "content_type": content_type,
         "created_by": created_by, "origin": origin},
        sort_keys=True, ensure_ascii=False,
    )


def _contract_json(
    name: str,
    inputs: list[str],
    outputs: list[str],
    activation: str,
    parent_id: Optional[str] = None,
    description: str = "",
    budget: int = 0,
    tool_scope: Optional[list[str]] = None,
    origin: str = "human",
) -> str:
    return json.dumps(
        {"parent_id": parent_id, "name": name, "description": description,
         "inputs": sorted(inputs), "outputs": sorted(outputs),
         "activation": activation, "budget": budget,
         "tool_scope": sorted(tool_scope or []), "origin": origin},
        sort_keys=True, ensure_ascii=False,
    )


def _asset_names_for(asset_ids: list[str], store: MemoryStore) -> list[str]:
    return [
        (store.get_asset(aid).name if store.get_asset(aid) else aid)
        for aid in asset_ids
    ]


def _run_demo(goal: str) -> tuple[MemoryStore, TraceStore, Contract]:
    """Run the build_report hallucination containment demo."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()
    raw_output = (
        f"final_report: Report content for goal '{goal}'\n"
        f"citation_summary: Citation summary for goal '{goal}'"
    )
    worker.set_output("build_report", raw_output)

    data_canonical = _asset_json("data_file", "Sample data for report generation")
    citation_canonical = _asset_json("citation_db", "Sample citation database")

    data_file = Asset(
        id=asset_id(data_canonical), name="data_file",
        content="Sample data for report generation",
    )
    citation_db = Asset(
        id=asset_id(citation_canonical), name="citation_db",
        content="Sample citation database",
    )

    contract_canonical = _contract_json(
        name="build_report",
        inputs=["data_file", "citation_db"],
        outputs=["final_report"],
        activation="data_file AND citation_db",
        budget=5,
    )
    contract = Contract(
        id=contract_id(contract_canonical), name="build_report",
        inputs=["data_file", "citation_db"], outputs=["final_report"],
        activation="data_file AND citation_db",
        budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(data_file)
    engine.add_asset(citation_db)
    engine.run()

    return store, trace_store, contract


@click.group()
def cli() -> None:
    """aig — Aigineering ACM runtime CLI."""


@cli.command()
@click.argument("goal")
def run(goal: str) -> None:
    """Execute a demo contract and display the commitment boundary result."""
    store, trace_store, contract = _run_demo(goal)
    entries = trace_store.get_by_contract(contract.id)
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


@cli.command()
@click.option("--contract", "contract_filter", default=None, help="Filter by contract ID")
def trace(contract_filter: Optional[str]) -> None:
    """Show the built-in demo trace timeline, including rejected fragments.
    
    Note: Currently runs the demo inline. Persisted trace coming in v0.2.
    """
    _, trace_store, _ = _run_demo("demo")
    entries = (
        trace_store.get_by_contract(contract_filter)
        if contract_filter
        else trace_store.get_all()
    )
    if not entries:
        click.echo("No trace entries found.")
        return
    for entry in entries:
        _print_timeline_entry(entry)


def _print_timeline_entry(entry: TraceEntry) -> None:
    prefix = f"  {entry.event_type:<14}"
    if entry.event_type == "activation":
        click.echo(f"{prefix}← contract enabled because activation satisfied")
    elif entry.event_type == "disclosure":
        worker = entry.worker_id or "worker"
        assets = entry.disclosed_assets or []
        click.echo(f"{prefix}← {assets} → {worker}")
    elif entry.event_type == "projection":
        accepted = entry.accepted_fragments or []
        rejected = entry.rejected_fragments or []
        parts: list[str] = []
        if accepted:
            parts.append(f"accepted: {accepted}")
        if rejected:
            parts.append(f"REJECTED: {rejected}")
        click.echo(f"{prefix}← {' | '.join(parts)}")
    elif entry.event_type == "complete":
        click.echo(f"{prefix}← outputs satisfied")


@cli.command()
@click.option("--asset", "asset_id_filter", default=None, help="Asset ID to trace")
@click.option("--asset-name", "asset_name_filter", default=None, help="Asset name to trace")
def audit(
    asset_id_filter: Optional[str],
    asset_name_filter: Optional[str],
) -> None:
    """Show the built-in demo lineage from an asset back to activation.
    
    Note: Currently runs the demo inline. Persisted audit coming in v0.2.
    """
    store, trace_store, _ = _run_demo("demo")

    target_id: Optional[str] = None
    target_name: Optional[str] = None

    if asset_id_filter:
        asset = store.get_asset(asset_id_filter)
        if asset:
            target_id = asset_id_filter
            target_name = asset.name
        else:
            matches = store.get_assets_by_name(asset_id_filter)
            if not matches:
                click.echo(f"No asset found with id or name '{asset_id_filter}'")
                return
            target_id = matches[0].id
            target_name = matches[0].name
    elif asset_name_filter:
        matches = store.get_assets_by_name(asset_name_filter)
        if not matches:
            click.echo(f"No asset found with name '{asset_name_filter}'")
            return
        target_id = matches[0].id
        target_name = matches[0].name
    else:
        click.echo("Provide --asset <id> or --asset-name <name>")
        return

    if not target_id:
        click.echo("Could not determine target asset.")
        return

    _print_reverse_lineage(target_id, target_name or target_id, trace_store, store)


def _print_reverse_lineage(
    asset_id_val: str, asset_name: str, trace_store: TraceStore, store: MemoryStore
) -> None:
    lineage_entries = trace_store.get_reverse_lineage(asset_id_val)
    if not lineage_entries:
        click.echo(f"{asset_name}")
        click.echo("  (no lineage found)")
        return

    click.echo(asset_name)
    for entry in lineage_entries:
        indent = "  "
        if entry.event_type == "projection":
            click.echo(f"{indent}← projection from candidate by {entry.worker_id or 'worker'}")
            _follow_parents(entry, trace_store, store, indent + "  ")
        elif entry.event_type == "disclosure":
            names = _asset_names_for(entry.disclosed_assets, store)
            click.echo(f"{indent}← disclosure: {names}")
        elif entry.event_type == "activation":
            click.echo(f"{indent}← activation: conditions met")


def _follow_parents(
    entry: TraceEntry, trace_store: TraceStore, store: MemoryStore, indent: str, max_depth: int = 5
) -> None:
    current = entry
    for _ in range(max_depth):
        if not current.parent_id:
            break
        parent: Optional[TraceEntry] = None
        for e in trace_store.get_all():
            if e.id == current.parent_id:
                parent = e
                break
        if not parent:
            break
        if parent.event_type == "disclosure":
            names = _asset_names_for(parent.disclosed_assets, store)
            click.echo(f"{indent}← disclosure: {names}")
        elif parent.event_type == "activation":
            click.echo(f"{indent}← activation: conditions met")
        elif parent.event_type == "projection":
            click.echo(f"{indent}← projection from candidate by {parent.worker_id or 'worker'}")
        current = parent


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
