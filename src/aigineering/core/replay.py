"""Replay module — reconstruct runtime state from persisted session data."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aigineering.core.provenance import verify_asset_signature
from aigineering.core.session import SessionStore
from aigineering.core.store import JsonLStore
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import Session, TraceEntry


def replay_session(
    session_id: str,
    sessions_dir: str = ".aig/sessions",
    traces_dir: str = ".aig/traces",
    store_dir: str = ".aig/store",
) -> dict:
    """Read session manifest → load trace → validate consistency.

    Returns dict with keys:
      session, entries, accepted_count, rejected_count, consistent (bool).
    """
    # 1. Load Session from SessionStore
    session_store = SessionStore(sessions_dir=sessions_dir)
    session: Optional[Session] = session_store.get_session(session_id)
    if session is None:
        return {
            "session": None,
            "error": f"Session '{session_id}' not found in {sessions_dir}",
        }

    # 2. Find the matching trace file
    trace_dir = Path(traces_dir)
    trace_store: Optional[JsonLTraceStore] = None
    entries: list[TraceEntry] = []

    # Try direct match: traces_dir/{session_id}.jsonl
    direct_path = trace_dir / f"{session_id}.jsonl"
    if direct_path.exists():
        trace_store = JsonLTraceStore(str(direct_path))
        entries = trace_store.get_all()
    else:
        # Search all session_*.jsonl files for one containing the session's trace_ids
        trace_id_set = set(session.trace_ids)
        if trace_dir.exists() and trace_id_set:
            for fp in sorted(
                trace_dir.glob("session_*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                candidate_store = JsonLTraceStore(str(fp))
                candidate_ids = {e.id for e in candidate_store.get_all()}
                # Match if the session's trace_ids are a subset of candidate's entries
                if trace_id_set <= candidate_ids or trace_id_set & candidate_ids:
                    trace_store = candidate_store
                    entries = trace_store.get_all()
                    break

    if trace_store is None or not entries:
        return {
            "session": session,
            "error": f"No matching trace file found for session '{session_id}'",
        }

    # 3. Group entries by event_type
    by_event: dict[str, list[TraceEntry]] = {}
    for entry in entries:
        by_event.setdefault(entry.event_type, []).append(entry)

    # 4. Count accepted vs rejected fragments across asset-producing entries
    accepted_count = 0
    rejected_count = 0
    accepted_ids: list[str] = []

    for entry in entries:
        if entry.event_type in ("projection", "tool_executed"):
            accepted_count += len(entry.accepted_fragments)
            rejected_count += len(entry.rejected_fragments)
            accepted_ids.extend(entry.accepted_fragments)

    # 5. Validate: every accepted asset_id appears exactly once across projections
    seen: set[str] = set()
    duplicates: list[str] = []
    for aid in accepted_ids:
        if aid in seen:
            duplicates.append(aid)
        else:
            seen.add(aid)

    signature_mismatches: list[str] = []
    store_path = Path(store_dir)
    assets_path = store_path / "assets.jsonl"
    contracts_path = store_path / "contracts.jsonl"
    if assets_path.exists():
        asset_store = JsonLStore(str(assets_path), str(contracts_path))
        for aid in sorted(seen):
            asset = asset_store.get_asset(aid)
            if asset is not None and not verify_asset_signature(asset):
                signature_mismatches.append(aid)

    consistent = len(duplicates) == 0 and len(signature_mismatches) == 0

    return {
        "session": session,
        "entries": entries,
        "by_event": by_event,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "consistent": consistent,
        "duplicate_ids": duplicates if duplicates else None,
        "signature_mismatches": (
            signature_mismatches if signature_mismatches else None
        ),
    }


def replay_all(
    sessions_dir: str = ".aig/sessions",
    traces_dir: str = ".aig/traces",
    store_dir: str = ".aig/store",
) -> list[dict]:
    """Replay all stored sessions. Returns list of result dicts."""
    session_store = SessionStore(sessions_dir=sessions_dir)
    sessions = session_store.list_sessions()
    results: list[dict] = []
    for s in sessions:
        result = replay_session(
            s.id,
            sessions_dir=sessions_dir,
            traces_dir=traces_dir,
            store_dir=store_dir,
        )
        results.append(result)
    return results
