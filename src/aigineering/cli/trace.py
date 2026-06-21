"""aig trace and aig audit commands."""

from __future__ import annotations

import json
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
@click.option(
    "--contract", "contract_filter", default=None, help="Filter by contract ID"
)
@click.option(
    "--session", "session_id", default=None, help="Read from a specific session ID"
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
@click.option(
    "--tree",
    "tree_output",
    is_flag=True,
    default=False,
    help="Show hierarchical parent→child→method→tool chain view.",
)
@click.option(
    "--dag",
    "dag_output",
    is_flag=True,
    default=False,
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
    payload = [trace_entry_to_dict(e) for e in entries]
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
    elif entry.event_type == "asset_injected":
        name = entry.relation_target or "(unnamed)"
        extra = _parse_asset_injected_audit(entry)
        label = f"injected asset: {name}"
        if extra:
            label += f" {extra}"
        click.echo(f"{prefix}← {label}")
    elif entry.event_type == "asset_injected_protected_override":
        name = entry.relation_target or "(unnamed)"
        click.echo(f"{prefix}← protected override: {name}")
    elif entry.event_type == "complete":
        click.echo(f"{prefix}← outputs satisfied")


def _parse_asset_injected_audit(entry: TraceEntry) -> str:
    """Extract audit metadata from an asset_injected trace entry."""
    for af in entry.accepted_fragments or []:
        try:
            data = json.loads(af)
            if isinstance(data, dict) and "asset_id" in data:
                aid = data.get("asset_id", "")[:16]
                parts = [f"({aid}"]
                if "origin" in data:
                    parts.append(data["origin"])
                if "trust_tier" in data:
                    parts.append(data["trust_tier"])
                parts.append(")")
                return " ".join(parts)
        except (json.JSONDecodeError, TypeError):
            continue
    return ""


# ---------------------------------------------------------------------------
# Tree view
# ---------------------------------------------------------------------------


def _build_contract_tree(
    entries: list[TraceEntry],
) -> dict[str, dict]:
    """Build a projection tree from trace entries: {contract_id: {entry, children}}.

    Parent→child relationships are derived from method_scheduled and
    contracts_expanded events.  This is a pure projection — no runtime truth
    is stored, it is computed on the fly from trace entries every time.
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

    def _collect_ids(node: dict) -> set[str]:
        ids = {node["contract_id"]}
        for child in node.get("children", []):
            ids |= _collect_ids(child)
        return ids

    tree: dict[str, dict] = {}
    existing_ids: set[str] = set()
    for root_id in sorted(roots):
        node = _build_node(root_id)
        tree[root_id] = node
        existing_ids |= _collect_ids(node)
    for child_id in child_parent:
        if child_id not in roots and child_id not in existing_ids:
            tree[child_id] = _build_node(child_id)
    return tree


def _print_trace_tree(entries: list[TraceEntry]) -> None:
    """Print a hierarchical tree view with tree-drawing characters.

    Uses ``|   `` vertical bars and ``├──`` / ``└──`` branch markers.
    Shows accepted/rejected asset counts per contract and the full
    parent→child→method→tool chain.
    """
    tree = _build_contract_tree(entries)

    def _contract_counts(node_entries: list[TraceEntry]) -> tuple[int, int]:
        """Return (accepted_count, rejected_count) for a contract's entries."""
        accepted = 0
        rejected = 0
        for e in node_entries:
            if e.event_type == "projection":
                accepted += len(e.accepted_fragments or [])
                rejected += len(e.rejected_fragments or [])
        return accepted, rejected

    def _print_node(
        contract_id: str,
        node: dict,
        indent_prefix: str,
        is_last: bool,
    ) -> None:
        node_entries: list[TraceEntry] = node.get("entries", [])
        children: list[dict] = node.get("children", [])
        acc, rej = _contract_counts(node_entries)
        counts = f" (accepted: {acc}, rejected: {rej})" if acc or rej else ""

        branch = "└── " if is_last else "├── "
        click.echo(f"{indent_prefix}{branch}contract: {contract_id}{counts}")

        entry_indent = indent_prefix + ("    " if is_last else "│   ")
        for e in node_entries:
            label = _entry_short_label(e)
            click.echo(f"{entry_indent}[{e.event_type}] {label}")

        for i, child in enumerate(children):
            child_is_last = i == len(children) - 1
            _print_node(
                child["contract_id"],
                child,
                entry_indent,
                child_is_last,
            )

    def _has_content(node: dict) -> bool:
        return bool(node.get("entries")) or bool(node.get("children"))

    top_level = [(cid, n) for cid, n in tree.items() if _has_content(n)]

    for i, (cid, node) in enumerate(top_level):
        is_last = i == len(top_level) - 1
        _print_node(cid, node, "", is_last)


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
    elif entry.event_type == "asset_injected":
        name = entry.relation_target or "unnamed"
        extra = _parse_asset_injected_audit(entry)
        label = f"injected asset: {name}"
        if extra:
            label += f" {extra}"
        return label
    elif entry.event_type == "asset_injected_protected_override":
        name = entry.relation_target or "unnamed"
        return f"protected override: {name}"
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


def _contract_status_map(entries: list[TraceEntry]) -> dict[str, str]:
    """Derive contract status from trace entries (pure projection).

    Status is determined from trace events only:
      - completed: contract has a ``complete`` event
      - suspended: contract scheduled a method but has not yet resumed
      - active: contract has events but no ``complete`` (still in progress)
    """
    by_contract: dict[str, list[TraceEntry]] = {}
    for e in entries:
        by_contract.setdefault(e.contract_id, []).append(e)

    status: dict[str, str] = {}
    for cid, evts in by_contract.items():
        has_complete = any(e.event_type == "complete" for e in evts)
        if has_complete:
            status[cid] = "completed"
            continue
        has_scheduled = any(e.event_type == "method_scheduled" for e in evts)
        has_resumed = any(e.event_type == "method_resumed" for e in evts)
        if has_scheduled and not has_resumed:
            status[cid] = "suspended"
        else:
            status[cid] = "active"
    return status


def _print_trace_dag(entries: list[TraceEntry]) -> None:
    """Print a Mermaid flowchart showing the contract dependency graph.

    Nodes are color-coded by status derived from trace events:
      - completed: green (#90EE90)
      - suspended: yellow (#FFD700)
      - active: blue (#87CEEB)

    Edges show the relation type (plan, tool, expanded, etc).
    This is a pure projection — computed on the fly every time.
    """
    edges = _build_contract_dag(entries)
    status = _contract_status_map(entries)

    if not edges:
        click.echo("(no parent→child contract edges found)")
        return

    all_contract_ids: set[str] = set()
    for parent, _rel, child in edges:
        all_contract_ids.add(parent)
        all_contract_ids.add(child)

    lines: list[str] = []
    lines.append("```mermaid")
    lines.append("flowchart TD")

    for cid in sorted(all_contract_ids):
        st = status.get(cid, "active")
        safe_id = cid.replace(":", "_").replace("-", "_").replace("/", "_")
        label = cid if len(cid) <= 40 else cid[:37] + "..."
        lines.append(f'    {safe_id}["{label}<br/>{st}"]')

    for parent, rel, child in edges:
        safe_parent = parent.replace(":", "_").replace("-", "_").replace("/", "_")
        safe_child = child.replace(":", "_").replace("-", "_").replace("/", "_")
        lines.append(f'    {safe_parent} -->|"{rel}"| {safe_child}')

    lines.append("")
    lines.append("    classDef completed fill:#90EE90,stroke:#333")
    lines.append("    classDef suspended fill:#FFD700,stroke:#333")
    lines.append("    classDef active fill:#87CEEB,stroke:#333")
    lines.append("")

    for cid in sorted(all_contract_ids):
        st = status.get(cid, "active")
        safe_id = cid.replace(":", "_").replace("-", "_").replace("/", "_")
        lines.append(f"    class {safe_id} {st}")

    lines.append("```")
    click.echo("\n".join(lines))


# ---------------------------------------------------------------------------
# audit command
# ---------------------------------------------------------------------------


@click.command("audit")
@click.option("--asset", "asset_id_filter", default=None, help="Asset ID to trace")
@click.option(
    "--asset-name", "asset_name_filter", default=None, help="Asset name to trace"
)
@click.option(
    "--session", "session_id", default=None, help="Read from a specific session ID"
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
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
            asset_id_filter,
            jsonl_store,
            name_map,
        )
        if target_id is None:
            target_id, target_name = _resolve_asset_by_name(
                asset_id_filter,
                jsonl_store,
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
            asset_name_filter,
            jsonl_store,
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
        target_id,
        target_name or target_id,
        jsonl_store,
        name_map,
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
    _output_json(
        {
            "asset_id": asset_id,
            "asset_name": asset_name,
            "lineage": [trace_entry_to_dict(e) for e in lineage],
        }
    )


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
            click.echo(
                f"{indent}← projection from candidate by {entry.worker_id or 'worker'}"
            )
            if entry.accepted_fragments:
                accepted_names = entry.accepted_asset_names or ["?"] * len(
                    entry.accepted_fragments
                )
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
            click.echo(
                f"{indent}← projection from candidate by {parent.worker_id or 'worker'}"
            )
        current = parent
