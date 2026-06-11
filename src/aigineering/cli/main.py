"""aig — Aigineering command-line interface."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import click

from aigineering.core.engine import Engine
from aigineering.core.ids import hash_asset_content, hash_asset_definition, hash_contract
from aigineering.core.replay import replay_all, replay_session
from aigineering.core.session import SessionStore
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.idempotency_store import IdempotencyStore
from aigineering.core.store import JsonLStore, MemoryStore, StoreProtocol
from aigineering.core.submit import SubmitConflictError, submit_candidate
from aigineering.core.sufficiency import check_sufficiency
from aigineering.core.trace import JsonLTraceStore, MemoryTraceStore, TraceStoreProtocol
from aigineering.agent.llm import LLMWorker
from aigineering.agent.mock import MockWorker
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.types import Asset, Contract, Session, TraceEntry
from aigineering.protocol.wire import asset_to_dict, contract_to_dict, session_to_dict, trace_entry_to_dict


def _get_trace_dir() -> Path:
    """Return the trace directory (created lazily on first write)."""
    return Path(".aig/traces")


def _get_store_dir() -> Path:
    """Return the persistent asset/contract store directory."""
    return Path(".aig/store")


def _persistent_store() -> JsonLStore:
    """Create the default local persistent store."""
    store_dir = _get_store_dir()
    return JsonLStore(
        str(store_dir / "assets.jsonl"),
        str(store_dir / "contracts.jsonl"),
    )


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


def _asset_names_for(
    asset_ids: list[str],
    resolver: StoreProtocol | dict[str, str],
) -> list[str]:
    """Resolve asset IDs to names via a store or an id→name dict."""
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


def _find_trace_for_session(
    session_id: str,
    sessions_dir: str = ".aig/sessions",
    traces_dir: str = ".aig/traces",
) -> tuple[Optional[JsonLTraceStore], Optional[list[TraceEntry]]]:
    """Find the trace file matching a session manifest."""
    session_store = SessionStore(sessions_dir=sessions_dir)
    session = session_store.get_session(session_id)
    if session is None:
        return None, None

    trace_dir = Path(traces_dir)
    trace_id_set = set(session.trace_ids)

    # Try direct match first
    direct_path = trace_dir / f"{session_id}.jsonl"
    if direct_path.exists():
        store = JsonLTraceStore(str(direct_path))
        return store, store.get_all()

    # Search all trace files for matching entries
    if trace_dir.exists() and trace_id_set:
        for fp in sorted(
            trace_dir.glob("session_*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            candidate = JsonLTraceStore(str(fp))
            candidate_ids = {e.id for e in candidate.get_all()}
            if trace_id_set <= candidate_ids or trace_id_set & candidate_ids:
                entries = candidate.get_all()
                return candidate, entries

    return None, None


def _run_demo(
    goal: str,
    trace_store: TraceStoreProtocol | None = None,
    store: StoreProtocol | None = None,
    worker_kind: str = "mock",
    model: Optional[str] = None,
    base_url: str = "https://api.openai.com/v1",
) -> tuple[StoreProtocol, TraceStoreProtocol, Contract]:
    """Run the build_report hallucination containment demo."""
    if store is None:
        store = MemoryStore()
    if trace_store is None:
        trace_store = MemoryTraceStore()
    worker = _build_worker(worker_kind, model, base_url)
    if isinstance(worker, MockWorker):
        raw_output = (
            f"final_report: Report content for goal '{goal}'\n"
            f"citation_summary: Citation summary for goal '{goal}'"
        )
        worker.set_output("build_report", raw_output)

    data_file = Asset(
        id=hash_asset_content("data_file", "Sample data for report generation"),
        name="data_file",
        content="Sample data for report generation",
        definition_hash=hash_asset_definition("data_file"),
        content_hash=hash_asset_content("data_file", "Sample data for report generation"),
        origin="human",
        trust_tier="human",
    )
    citation_db = Asset(
        id=hash_asset_content("citation_db", "Sample citation database"),
        name="citation_db",
        content="Sample citation database",
        definition_hash=hash_asset_definition("citation_db"),
        content_hash=hash_asset_content("citation_db", "Sample citation database"),
        origin="human",
        trust_tier="human",
    )

    contract = Contract(
        id=hash_contract(
            name="build_report",
            description=f"Build a report for goal: {goal}",
            inputs=["data_file", "citation_db"],
            outputs=["final_report"],
            activation="data_file AND citation_db",
            budget=5,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name="build_report",
        inputs=["data_file", "citation_db"],
        outputs=["final_report"],
        activation="data_file AND citation_db",
        budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(data_file)
    engine.add_asset(citation_db)
    engine.run()

    return store, trace_store, contract


def _build_worker(
    worker_kind: str,
    model: Optional[str],
    base_url: str,
) -> MockWorker | LLMWorker:
    if worker_kind == "mock":
        return MockWorker()
    if worker_kind == "llm":
        if not model:
            raise click.ClickException("--model is required when --worker llm")
        return LLMWorker(model=model, base_url=base_url)
    raise click.ClickException(f"unsupported worker: {worker_kind}")


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
# JSON output helpers
# ---------------------------------------------------------------------------


def _redact_sealed(data: dict) -> dict:
    """Return a copy of *data* with sealed fields redacted.

    Removes ``config_snapshot`` and ``worker_snapshot`` entirely so API keys
    are never leaked into JSON output.
    """
    return {k: v for k, v in data.items() if k not in ("config_snapshot", "worker_snapshot")}


def _output_json(payload: object) -> None:
    """Write *payload* as indented JSON to stdout."""
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _output_run_json(
    contract_id: str,
    trace_ids: list[str],
    session_id: str,
    entries: list[TraceEntry],
) -> None:
    status = "complete" if entries and any(
        e.event_type == "complete" for e in entries
    ) else "incomplete"
    _output_json({
        "contract_id": contract_id,
        "session_id": session_id,
        "trace_ids": trace_ids,
        "status": status,
    })


def _output_trace_json(entries: list[TraceEntry]) -> None:
    payload = [
        trace_entry_to_dict(e) for e in entries
    ]
    _output_json(payload)


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


def _output_replay_json(result: dict) -> None:
    session = result.get("session")
    entries = result.get("entries", [])
    payload: dict = {
        "session": (
            _redact_sealed(session_to_dict(session))
            if session is not None else None
        ),
        "entries": [trace_entry_to_dict(e) for e in entries],
        "accepted_count": result.get("accepted_count", 0),
        "rejected_count": result.get("rejected_count", 0),
        "consistent": result.get("consistent", False),
    }
    by_event = result.get("by_event", {})
    if by_event:
        payload["by_event"] = {
            k: len(v) for k, v in by_event.items()
        }
    duplicates = result.get("duplicate_ids")
    if duplicates:
        payload["duplicate_ids"] = duplicates
    _output_json(payload)


def _build_replay_json_result(result: dict) -> dict:
    session = result.get("session")
    entries = result.get("entries", [])
    payload: dict = {
        "session": (
            _redact_sealed(session_to_dict(session))
            if session is not None else None
        ),
        "entries": [trace_entry_to_dict(e) for e in entries],
        "accepted_count": result.get("accepted_count", 0),
        "rejected_count": result.get("rejected_count", 0),
        "consistent": result.get("consistent", False),
    }
    by_event = result.get("by_event", {})
    if by_event:
        payload["by_event"] = {
            k: len(v) for k, v in by_event.items()
        }
    duplicates = result.get("duplicate_ids")
    if duplicates:
        payload["duplicate_ids"] = duplicates
    return payload


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """aig — Aigineering ACM runtime CLI."""


@cli.command()
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
    "--json", "json_output", is_flag=True, default=False,
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


@cli.command()
@click.option("--contract", "contract_filter", default=None, help="Filter by contract ID")
@click.option("--session", "session_id", default=None, help="Read from a specific session ID")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def trace(
    contract_filter: Optional[str],
    session_id: Optional[str],
    json_output: bool,
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


@cli.command()
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


@cli.command()
@click.argument("session_id", required=False)
@click.option("--all", "replay_all_flag", is_flag=True, default=False, help="Replay all stored sessions")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def replay(
    session_id: Optional[str],
    replay_all_flag: bool,
    json_output: bool,
) -> None:
    """Replay a session from persisted data and validate consistency."""
    if replay_all_flag:
        results = replay_all()
        if not results:
            if json_output:
                _output_json([])
            else:
                click.echo("No sessions found.")
            return
        if json_output:
            _output_json([_build_replay_json_result(r) for r in results])
            return
        for r in results:
            _print_replay_result(r)
            click.echo("")
        return

    if not session_id:
        if json_output:
            _output_json({"error": "Usage: aig replay <session_id>  or  aig replay --all"})
        else:
            click.echo("Usage: aig replay <session_id>  or  aig replay --all")
        return

    result = replay_session(session_id)
    if "error" in result:
        if json_output:
            _output_json({"error": result["error"]})
        else:
            click.echo(result["error"])
        return

    if json_output:
        _output_replay_json(result)
        return

    _print_replay_result(result)


def _print_replay_result(result: dict) -> None:
    session = result.get("session")
    if session is None:
        return

    click.echo(f"Session: {session.id}")
    click.echo(f"  Root contract: {session.root_contract_id}")
    click.echo(f"  Created: {session.created_at}")

    entries = result.get("entries", [])
    by_event = result.get("by_event", {})

    click.echo(f"  Trace entries: {len(entries)}")
    for event_type in sorted(by_event):
        click.echo(f"    {event_type}: {len(by_event[event_type])}")

    accepted = result.get("accepted_count", 0)
    rejected = result.get("rejected_count", 0)
    click.echo(f"  Accepted fragments: {accepted}")
    click.echo(f"  Rejected fragments: {rejected}")

    consistent = result.get("consistent", False)
    if consistent:
        click.echo(f"  Consistency: ✓ no duplicate asset IDs")
    else:
        duplicates = result.get("duplicate_ids", [])
        click.echo(f"  Consistency: ✗ duplicate asset IDs: {duplicates}")

    click.echo("")
    click.echo("  Timeline:")
    for entry in entries:
        marker = "✓" if entry.event_type in ("activation", "complete") else "→"
        if entry.event_type == "projection":
            if entry.rejected_fragments and not entry.accepted_fragments:
                marker = "✗"
        click.echo(f"    {marker} [{entry.event_type}]", nl=False)
        if entry.event_type == "activation":
            click.echo(f" contract activated (budget: {entry.budget_remaining})")
        elif entry.event_type == "disclosure":
            worker = entry.worker_id or "worker"
            assets = entry.disclosed_assets or []
            click.echo(f" {assets} → {worker}")
        elif entry.event_type == "projection":
            accepted = len(entry.accepted_fragments)
            rejected_count = len(entry.rejected_fragments)
            click.echo(f" +{accepted} accepted, -{rejected_count} rejected")
        elif entry.event_type == "complete":
            click.echo(f" outputs satisfied")


@cli.command()
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


@cli.command()
@click.option("--contract", "contract_id", required=True, help="Contract ID to check readiness for")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def readiness(
    contract_id: str,
    json_output: bool,
) -> None:
    """Check contract readiness and produce a sufficiency report."""
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        if json_output:
            _output_json({"error": f"Contract '{contract_id}' not found."})
        else:
            click.echo(f"Contract '{contract_id}' not found.")
        return

    report = check_sufficiency(contract, store)

    if json_output:
        _output_json(report)
        return

    click.echo(f"Readiness report for contract '{contract.name}' ({contract.id}):")
    click.echo(f"  Recommendation: {report['recommendation']}")
    click.echo(f"  Sufficient:      {report['sufficiency_ok']}")
    if report["missing_inputs"]:
        click.echo(f"  Missing inputs:  {report['missing_inputs']}")
    if report["stale_assets"]:
        click.echo(f"  Stale assets:    {report['stale_assets']}")
    if report["version_conflicts"]:
        click.echo(f"  Version conflicts:")
        for vc in report["version_conflicts"]:
            click.echo(f"    def_hash={vc['definition_hash']} names={vc['names']}")
    if report["trust_gaps"]:
        click.echo(f"  Trust gaps:      {report['trust_gaps']}")
    if report["signature_gaps"]:
        click.echo(f"  Signature gaps:  {report['signature_gaps']}")


# ---------------------------------------------------------------------------
# Worker commands
# ---------------------------------------------------------------------------


def _get_idempotency_path() -> str:
    """Return the path to the idempotency store JSONL file."""
    return str(_get_store_dir() / "idempotency.jsonl")


@cli.group()
def worker() -> None:
    """Operational worker commands for contract execution."""


@worker.command("package")
@click.option("--contract", "contract_id", required=True, help="Contract ID to build package for")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def worker_package(contract_id: str, json_output: bool) -> None:
    """Create a WorkerPackage for a contract from the durable store."""
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        if json_output:
            _output_json({"error": f"Contract '{contract_id}' not found."})
        else:
            click.echo(f"Contract '{contract_id}' not found.")
        return

    scope = compute_disclosure(contract, store)
    pkg = WorkerPackage(
        contract_id=contract.id,
        contract=contract_to_dict(contract),
        disclosed_assets=tuple(asset_to_dict(a) for a in scope),
        method_context_assets=(),
        tool_scope=contract.tool_scope,
        budget_remaining=contract.budget,
    )

    if json_output:
        result = json.loads(pkg.to_json())
        _output_json(result)
        return

    click.echo(pkg.to_json())


@worker.command("submit")
@click.option("--json", "envelope_json", required=True, help="CandidateEnvelope JSON string")
@click.option(
    "--idempotency-key",
    default=None,
    help="Idempotency key for deduplication",
)
def worker_submit(envelope_json: str, idempotency_key: Optional[str]) -> None:
    """Submit a candidate envelope for projection and commitment.

    ENVELOPE_JSON must be a valid CandidateEnvelope serialized as JSON.
    Output is always JSON.
    """
    try:
        envelope = CandidateEnvelope.from_json(envelope_json)
    except (ValueError, json.JSONDecodeError) as e:
        _output_json({"error": f"Invalid envelope: {e}"})
        return

    store = _persistent_store()

    if store.get_contract(envelope.contract_id) is None:
        _output_json({"error": f"Contract '{envelope.contract_id}' not found."})
        return

    idem_path = _get_idempotency_path()
    idem = IdempotencyStore(idem_path)

    # Use a per-contract trace file for operational submissions
    trace_dir = _get_trace_dir()
    trace_path = str(trace_dir / f"worker_{envelope.contract_id}.jsonl")
    trace_store = JsonLTraceStore(trace_path)

    try:
        result = submit_candidate(
            envelope=envelope,
            store=store,
            trace_store=trace_store,
            idempotency_store=idem,
            idempotency_key=idempotency_key or "",
        )
    except SubmitConflictError as e:
        _output_json({"error": str(e), "status": "conflict"})
        return

    # Redact sealed config from result
    result = _redact_sealed(result)
    _output_json(result)


@cli.command()
@click.option(
    "--definition-hash", "def_hash", required=True,
    help="Definition hash (def:<hex>) to verify content hashes for.",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def verify(def_hash: str, json_output: bool) -> None:
    """Batch-verify content hashes for all assets under a definition hash."""
    store = _persistent_store()
    result = batch_verify_definition(store, def_hash)

    if json_output:
        _output_json(result)
        return

    click.echo(f"Definition: {def_hash}")
    click.echo(f"  Pass: {result['pass_count']}  Fail: {result['fail_count']}")
    for r in result["results"]:
        status = "✓" if r["valid"] else "✗"
        click.echo(f"  {status} {r['asset_id']}")
        if not r["valid"]:
            if r.get("expected_content_hash") != r.get("content_hash"):
                click.echo(
                    f"    content_hash mismatch: "
                    f"stored={r['content_hash']} expected={r['expected_content_hash']}"
                )
            if r.get("expected_definition_hash") != r.get("definition_hash"):
                click.echo(
                    f"    definition_hash mismatch: "
                    f"stored={r['definition_hash']} expected={r['expected_definition_hash']}"
                )


@cli.group()
def session() -> None:
    """Manage session manifests."""


@session.command("ls")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def session_ls(json_output: bool) -> None:
    """List sessions with id and created_at."""
    store = SessionStore()
    sessions = store.list_sessions()
    if json_output:
        payload = [
            _redact_sealed(session_to_dict(s)) for s in sessions
        ]
        _output_json(payload)
        return
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
