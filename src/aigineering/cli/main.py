"""aig — Aigineering command-line interface."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import click

from aigineering.core.engine import Engine
from aigineering.core.ids import asset_id, contract_id
from aigineering.core.session import SessionStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import JsonLTraceStore, MemoryTraceStore, TraceStoreProtocol
from aigineering.agent.mock import MockWorker
from aigineering.protocol.types import Asset, Contract, Session, TraceEntry


def _get_trace_dir() -> Path:
    """Return the trace directory (created lazily on first write)."""
    return Path(".aig/traces")


def _session_id() -> str:
    """Return a nanosecond-timestamp session identifier to avoid same-second collisions."""
    return f"session_{time.time_ns()}"


def _latest_session_file() -> Optional[Path]:
    """Return the newest session_*.jsonl file in the trace dir, or None."""
    trace_dir = _get_trace_dir()
    if not trace_dir.exists():
        return None
    files = sorted(
        trace_dir.glob("session_*.jsonl"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    return files[0] if files else None


def _build_asset_name_map(entries: list[TraceEntry]) -> dict[str, str]:
    """Build an asset-id → asset-name mapping from projection entries."""
    name_map: dict[str, str] = {}
    for entry in entries:
        if entry.event_type == "projection":
            names = entry.accepted_asset_names or []
            frags = entry.accepted_fragments or []
            for i, aid in enumerate(frags):
                if i < len(names):
                    name_map[aid] = names[i]
    return name_map


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


def _asset_names_for(
    asset_ids: list[str],
    resolver: MemoryStore | dict[str, str],
) -> list[str]:
    """Resolve asset IDs to names via a MemoryStore or an id→name dict."""
    if isinstance(resolver, dict):
        return [resolver.get(aid, aid) for aid in asset_ids]
    return [
        (resolver.get_asset(aid).name if resolver.get_asset(aid) else aid)
        for aid in asset_ids
    ]


def _parse_rejected_fragment(rf: str) -> tuple[str, str]:
    """Parse a rejected_fragment string to extract category and display text.

    Format: "[category] name: reason" or legacy "name: reason".
    Returns (category, rest_of_text).
    """
    if rf.startswith("[") and "]" in rf:
        end = rf.index("]")
        return rf[1:end], rf[end + 1:].strip()
    return "unknown", rf


def _run_demo(
    goal: str,
    trace_store: TraceStoreProtocol | None = None,
) -> tuple[MemoryStore, TraceStoreProtocol, Contract]:
    """Run the build_report hallucination containment demo."""
    store = MemoryStore()
    if trace_store is None:
        trace_store = MemoryTraceStore()
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


# ---------------------------------------------------------------------------
# _resolve_asset_from_trace helpers (JSONL mode — no MemoryStore needed)
# ---------------------------------------------------------------------------

def _resolve_asset_by_id(
    asset_id_val: str,
    trace_store: TraceStoreProtocol,
    name_map: dict[str, str],
) -> tuple[Optional[str], Optional[str]]:
    """Return (id, name) if asset_id_val matches an accepted fragment id."""
    lineage = trace_store.get_reverse_lineage(asset_id_val)
    if lineage:
        return asset_id_val, name_map.get(asset_id_val, asset_id_val)
    return None, None


def _resolve_asset_by_name(
    asset_name: str,
    trace_store: TraceStoreProtocol,
) -> tuple[Optional[str], Optional[str]]:
    """Return (id, name) for the first projection entry whose
    accepted_asset_names contains *asset_name*."""
    for entry in trace_store.get_all():
        if entry.event_type == "projection":
            names = entry.accepted_asset_names or []
            frags = entry.accepted_fragments or []
            for i, name in enumerate(names):
                if name == asset_name and i < len(frags):
                    return frags[i], name
    return None, None


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """aig — Aigineering ACM runtime CLI."""


@cli.command()
@click.argument("goal")
def run(goal: str) -> None:
    """Execute a demo contract and persist the trace to JSONL."""
    trace_path = _get_trace_dir() / f"{_session_id()}.jsonl"
    jsonl_store = JsonLTraceStore(str(trace_path))
    store, trace_store, contract = _run_demo(goal, trace_store=jsonl_store)
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

    click.echo(f"Trace saved to {trace_path}")

    # ── Session manifest ───────────────────────────────────────────────────
    session_id = _session_id()
    contract_ids = [c.id for c in store.get_all_contracts()]
    asset_ids = [a.id for a in store.get_all_assets()]
    trace_ids = [e.id for e in jsonl_store.get_all()]
    session = Session(
        id=session_id,
        root_contract_id=contract.id,
        contract_ids=contract_ids,
        asset_ids=asset_ids,
        trace_ids=trace_ids,
    )
    session_store = SessionStore()
    session_store.create_session(session)


@cli.command()
@click.option("--contract", "contract_filter", default=None, help="Filter by contract ID")
def trace(contract_filter: Optional[str]) -> None:
    """Show the trace timeline from the latest session (or demo fallback)."""
    latest = _latest_session_file()
    if latest is not None:
        jsonl_store = JsonLTraceStore(str(latest))
        entries = (
            jsonl_store.get_by_contract(contract_filter)
            if contract_filter
            else jsonl_store.get_all()
        )
        if not entries:
            click.echo("No trace entries found.")
            return
        for entry in entries:
            _print_timeline_entry(entry)
    else:
        click.echo("No trace sessions found. Running demo…")
        click.echo("[note: persisted trace is now available — use `aig run` first]")
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
            tagged: list[str] = []
            for r in rejected:
                cat, rest = _parse_rejected_fragment(r)
                tagged.append(f"[{cat}] {rest}")
            parts.append(f"REJECTED: {tagged}")
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
    """Show lineage from an asset back to activation (from latest session or demo)."""
    latest = _latest_session_file()

    # ── JSONL path ────────────────────────────────────────────────────────
    if latest is not None:
        jsonl_store = JsonLTraceStore(str(latest))
        all_entries = jsonl_store.get_all()
        name_map = _build_asset_name_map(all_entries)

        target_id: Optional[str] = None
        target_name: Optional[str] = None

        if asset_id_filter:
            # Try direct ID first
            target_id, target_name = _resolve_asset_by_id(
                asset_id_filter, jsonl_store, name_map,
            )
            if target_id is None:
                # Fallback: treat as name
                target_id, target_name = _resolve_asset_by_name(
                    asset_id_filter, jsonl_store,
                )
            if target_id is None:
                click.echo(f"No asset found with id or name '{asset_id_filter}'")
                return
        elif asset_name_filter:
            target_id, target_name = _resolve_asset_by_name(
                asset_name_filter, jsonl_store,
            )
            if target_id is None:
                click.echo(f"No asset found with name '{asset_name_filter}'")
                return
        else:
            click.echo("Provide --asset <id> or --asset-name <name>")
            return

        if not target_id:
            click.echo("Could not determine target asset.")
            return

        _print_reverse_lineage(
            target_id, target_name or target_id, jsonl_store, name_map,
        )
        return

    # ── Demo fallback (original MemoryStore path) ─────────────────────────
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
    asset_id_val: str,
    asset_name: str,
    trace_store: TraceStoreProtocol,
    resolver: MemoryStore | dict[str, str],
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
            if entry.accepted_fragments:
                accepted_names = entry.accepted_asset_names or ["?"] * len(entry.accepted_fragments)
                for aid, aname in zip(entry.accepted_fragments, accepted_names):
                    click.echo(f"{indent}  ✓ accepted: {aname} ({aid})")
            if entry.rejected_fragments:
                for r in entry.rejected_fragments:
                    cat, rest = _parse_rejected_fragment(r)
                    click.echo(f"{indent}  ✗ rejected [{cat}]: {rest}")
            _follow_parents(entry, trace_store, resolver, indent + "  ")
        elif entry.event_type == "disclosure":
            names = _asset_names_for(entry.disclosed_assets, resolver)
            click.echo(f"{indent}← disclosure: {names}")
        elif entry.event_type == "activation":
            click.echo(f"{indent}← activation: conditions met")


def _follow_parents(
    entry: TraceEntry,
    trace_store: TraceStoreProtocol,
    resolver: MemoryStore | dict[str, str],
    indent: str,
    max_depth: int = 5,
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
            names = _asset_names_for(parent.disclosed_assets, resolver)
            click.echo(f"{indent}← disclosure: {names}")
        elif parent.event_type == "activation":
            click.echo(f"{indent}← activation: conditions met")
        elif parent.event_type == "projection":
            click.echo(f"{indent}← projection from candidate by {parent.worker_id or 'worker'}")
        current = parent


@cli.group()
def session() -> None:
    """Manage session manifests."""


@session.command("ls")
def session_ls() -> None:
    """List sessions with id and created_at."""
    store = SessionStore()
    sessions = store.list_sessions()
    if not sessions:
        click.echo("No sessions found.")
        return
    for s in sessions:
        click.echo(f"{s.id}  {s.created_at}")


@session.command("show")
@click.argument("session_id")
def session_show(session_id: str) -> None:
    """Show full session manifest."""
    store = SessionStore()
    s = store.get_session(session_id)
    if s is None:
        click.echo(f"Session '{session_id}' not found.")
        return
    click.echo(f"id:                {s.id}")
    click.echo(f"root_contract_id:  {s.root_contract_id}")
    click.echo(f"contract_ids:      {s.contract_ids}")
    click.echo(f"asset_ids:         {s.asset_ids}")
    click.echo(f"trace_ids:         {s.trace_ids}")
    click.echo(f"config_snapshot:   {s.config_snapshot}")
    click.echo(f"worker_snapshot:   {s.worker_snapshot}")
    click.echo(f"created_at:        {s.created_at}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
