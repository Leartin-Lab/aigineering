"""aig trace and aig audit commands."""

from __future__ import annotations

from typing import Optional

import click

from aigineering.cli._common import (
    _asset_names_for,
    _build_asset_name_map,
    _find_trace_for_session,
    _latest_session_file,
    _output_json,
    _parse_rejected_fragment,
)
from aigineering.core.trace import JsonLTraceStore, TraceStoreProtocol
from aigineering.core.store import StoreProtocol
from aigineering.protocol.types import TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict


# ---------------------------------------------------------------------------
# trace command
# ---------------------------------------------------------------------------

@click.command("trace")
@click.option("--contract", "contract_filter", default=None, help="Filter by contract ID")
@click.option("--session", "session_id", default=None, help="Read from a specific session ID")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
@click.option(
    "--tree", "tree_output", is_flag=True, default=False,
    help="Show hierarchical parent→child→method→tool chain view.",
)
@click.option(
    "--dag", "dag_output", is_flag=True, default=False,
    help="Show graph edges connecting parent→child contracts.",
)
def trace(
    contract_filter: Optional[str],
    session_id: Optional[str],
    json_output: bool,
    tree_output: bool,
    dag_output: bool,
) -> None:
    """Show the trace timeline from the latest session."""
    if session_id is not None:
        jsonl_store, entries = _find_trace_for_session(session_id)
        if jsonl_store is None:
            if json_output:
                _output_json({"error": f"Session '{session_id}' not found."})
            else:
                click.echo(f"Session '{session_id}' not found.")
            return
    else:
        latest = _latest_session_file()
        if latest is not None:
            jsonl_store = JsonLTraceStore(str(latest))
            entries = (
                jsonl_store.get_by_contract(contract_filter)
                if contract_filter
                else jsonl_store.get_all()
            )
        else:
            jsonl_store = None
            entries = []

    if jsonl_store is None or not entries:
        if json_output:
            _output_json([])
        else:
            click.echo("No sessions found. Use 'aig run <goal>' or 'aig demo'.")
        return

    if contract_filter:
        entries = jsonl_store.get_by_contract(contract_filter)

    if json_output:
        _output_trace_json(entries)
        return

    if tree_output:
        _print_trace_tree(entries)
        return

    if dag_output:
        _print_trace_dag(entries)
        return

    for entry in entries:
        _print_timeline_entry(entry)


# ---------------------------------------------------------------------------
# JSON / display helpers for trace
# ---------------------------------------------------------------------------

def _output_trace_json(entries: list[TraceEntry]) -> None:
    payload = [
        trace_entry_to_dict(e) for e in entries
    ]
    _output_json(payload)


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
    elif entry.event_type == "method_scheduled":
        method = entry.relation_type or "method"
        target = entry.relation_target or "(unknown)"
        worker = entry.worker_id or "worker"
        click.echo(f"{prefix}← /{method} scheduled {target} by {worker}")
    elif entry.event_type == "tool_executed":
        tool = entry.relation_target or "tool"
        status = entry.authority_result or "unknown"
        assets = entry.accepted_asset_names or []
        click.echo(f"{prefix}← {tool} {status}: {assets}")
    elif entry.event_type == "method_resumed":
        method = entry.relation_type or "method"
        assets = entry.disclosed_assets or []
        click.echo(f"{prefix}← parent resumed after /{method}: {assets}")
    elif entry.event_type == "contracts_expanded":
        targets = entry.relation_target or ""
        click.echo(f"{prefix}← planner expanded contracts: {targets}")
    elif entry.event_type == "complete":
        click.echo(f"{prefix}← outputs satisfied")


# ---------------------------------------------------------------------------
# Tree view
# ---------------------------------------------------------------------------

def _build_contract_tree(
    entries: list[TraceEntry],
) -> dict[str, dict]:
    """Build a projection tree from trace entries: {contract_id: {entry, children}}.

    Parent→child relationships are derived from method_scheduled and
    contracts_expanded events.  This is a pure projection — no runtime truth
    is stored, it is computed on the fly from trace entries.
    """
    by_contract: dict[str, list[TraceEntry]] = {}
    for e in entries:
        by_contract.setdefault(e.contract_id, []).append(e)

    child_parent: dict[str, str] = {}
    for e in entries:
        if e.event_type == "method_scheduled" and e.relation_target:
            child_parent[e.relation_target] = e.contract_id
        elif e.event_type == "contracts_expanded" and e.relation_target:
            for child_id in e.relation_target.replace(",", " ").split():
                child_id = child_id.strip()
                if child_id:
                    child_parent[child_id] = e.contract_id

    all_contract_ids = set(by_contract.keys())
    roots = all_contract_ids - set(child_parent.keys())

    def _build_node(cid: str) -> dict:
        node_entries = by_contract.get(cid, [])
        children = []
        for child_cid, parent_cid in child_parent.items():
            if parent_cid == cid:
                children.append(_build_node(child_cid))
        return {"contract_id": cid, "entries": node_entries, "children": children}

    tree: dict[str, dict] = {}
    for root_id in sorted(roots):
        tree[root_id] = _build_node(root_id)
    for child_id in child_parent:
        if child_id not in roots and child_id not in tree:
            tree[child_id] = _build_node(child_id)
    return tree


def _print_trace_tree(entries: list[TraceEntry]) -> None:
    """Print a hierarchical tree view derived from trace entries."""
    tree = _build_contract_tree(entries)

    def _print_node(contract_id: str, node: dict, indent: int) -> None:
        prefix = "  " * indent
        click.echo(f"{prefix}contract: {contract_id}")
        for e in node.get("entries", []):
            label = _entry_short_label(e)
            click.echo(f"{prefix}  [{e.event_type}] {label}")
        for child in node.get("children", []):
            _print_node(child["contract_id"], child, indent + 1)

    for cid, node in tree.items():
        if node.get("entries"):
            _print_node(cid, node, 0)
        elif node.get("children"):
            _print_node(cid, node, 0)


def _entry_short_label(entry: TraceEntry) -> str:
    """Return a compact label for a trace entry."""
    if entry.event_type == "activation":
        return "contract activated"
    elif entry.event_type == "disclosure":
        assets = entry.disclosed_assets or []
        return f"→ {list(assets)} to {entry.worker_id or 'worker'}"
    elif entry.event_type == "projection":
        accepted = entry.accepted_fragments or []
        rejected = entry.rejected_fragments or []
        parts = []
        if accepted:
            parts.append(f"accepted {list(accepted)}")
        if rejected:
            parts.append(f"rejected {len(rejected)}")
        return " | ".join(parts) if parts else "no output"
    elif entry.event_type == "method_scheduled":
        method = entry.relation_type or "method"
        target = entry.relation_target or "(unknown)"
        return f"/{method} → {target}"
    elif entry.event_type == "tool_executed":
        tool = entry.relation_target or "tool"
        status = entry.authority_result or "unknown"
        return f"{tool} ({status})"
    elif entry.event_type == "method_resumed":
        method = entry.relation_type or "method"
        return f"resumed after /{method}"
    elif entry.event_type == "contracts_expanded":
        target = entry.relation_target or ""
        return f"expanded → {target}"
    elif entry.event_type == "complete":
        return "complete"
    return ""


# ---------------------------------------------------------------------------
# DAG view
# ---------------------------------------------------------------------------

def _build_contract_dag(
    entries: list[TraceEntry],
) -> list[tuple[str, str, str]]:
    """Derive parent→child contract edges from trace entries.

    Returns list of (parent_contract_id, relation, child_contract_id).
    This is a pure projection — no runtime truth is stored.
    """
    edges: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for e in entries:
        if e.event_type == "method_scheduled" and e.relation_target:
            edge = (e.contract_id, e.relation_type or "method", e.relation_target)
            if edge not in seen:
                edges.append(edge)
                seen.add(edge)
        elif e.event_type == "contracts_expanded" and e.relation_target:
            for child_id in e.relation_target.replace(",", " ").split():
                child_id = child_id.strip()
                if child_id:
                    edge = (e.contract_id, "expanded", child_id)
                    if edge not in seen:
                        edges.append(edge)
                        seen.add(edge)

    return edges


def _print_trace_dag(entries: list[TraceEntry]) -> None:
    """Print a graph edge view connecting parent→child contracts."""
    edges = _build_contract_dag(entries)

    if not edges:
        click.echo("(no parent→child contract edges found)")
        return

    click.echo("Contract DAG edges:")
    for parent, rel, child in edges:
        click.echo(f"  {parent}  —[{rel}]→  {child}")


# ---------------------------------------------------------------------------
# audit command
# ---------------------------------------------------------------------------

@click.command("audit")
@click.option("--asset", "asset_id_filter", default=None, help="Asset ID to trace")
@click.option("--asset-name", "asset_name_filter", default=None, help="Asset name to trace")
@click.option("--session", "session_id", default=None, help="Read from a specific session ID")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def audit(
    asset_id_filter: Optional[str],
    asset_name_filter: Optional[str],
    session_id: Optional[str],
    json_output: bool,
) -> None:
    """Show lineage from an asset back to activation (from latest session)."""
    if session_id is not None:
        jsonl_store, all_entries = _find_trace_for_session(session_id)
        if jsonl_store is None:
            if json_output:
                _output_json({"error": f"Session '{session_id}' not found."})
            else:
                click.echo(f"Session '{session_id}' not found.")
            return
    else:
        latest = _latest_session_file()
        if latest is not None:
            jsonl_store = JsonLTraceStore(str(latest))
            all_entries = jsonl_store.get_all()
        else:
            if json_output:
                _output_json({"error": "No sessions found."})
            else:
                click.echo("No sessions found. Use 'aig run <goal>' or 'aig demo'.")
            return

    name_map = _build_asset_name_map(all_entries)

    target_id: Optional[str] = None
    target_name: Optional[str] = None

    if asset_id_filter:
        target_id, target_name = _resolve_asset_by_id(
            asset_id_filter, jsonl_store, name_map,
        )
        if target_id is None:
            target_id, target_name = _resolve_asset_by_name(
                asset_id_filter, jsonl_store,
            )
        if target_id is None:
            msg = f"No asset found with id or name '{asset_id_filter}'"
            if json_output:
                _output_json({"error": msg})
            else:
                click.echo(msg)
            return
    elif asset_name_filter:
        target_id, target_name = _resolve_asset_by_name(
            asset_name_filter, jsonl_store,
        )
        if target_id is None:
            msg = f"No asset found with name '{asset_name_filter}'"
            if json_output:
                _output_json({"error": msg})
            else:
                click.echo(msg)
            return
    else:
        msg = "Provide --asset <id> or --asset-name <name>"
        if json_output:
            _output_json({"error": msg})
        else:
            click.echo(msg)
        return

    if not target_id:
        msg = "Could not determine target asset."
        if json_output:
            _output_json({"error": msg})
        else:
            click.echo(msg)
        return

    lineage_entries = jsonl_store.get_reverse_lineage(target_id)

    if json_output:
        _output_audit_json(
            asset_id=target_id,
            asset_name=target_name or target_id,
            lineage=lineage_entries,
        )
        return

    _print_reverse_lineage(
        target_id, target_name or target_id, jsonl_store, name_map,
    )


# ---------------------------------------------------------------------------
# Audit JSON / display helpers
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


def _output_audit_json(
    asset_id: str,
    asset_name: str,
    lineage: list[TraceEntry],
) -> None:
    _output_json({
        "asset_id": asset_id,
        "asset_name": asset_name,
        "lineage": [trace_entry_to_dict(e) for e in lineage],
    })


def _print_reverse_lineage(
    asset_id_val: str,
    asset_name: str,
    trace_store: TraceStoreProtocol,
    resolver: StoreProtocol | dict[str, str],
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
    resolver: StoreProtocol | dict[str, str],
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
