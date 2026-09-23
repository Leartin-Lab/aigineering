"""Replay module — reconstruct runtime state from persisted session data.

.. admonition:: READ MODEL

   This module is a **READ MODEL** — it observes facts from the append-only
   trace, the durable store, and the session manifest.  It does **not** mutate
   any of those sources.  Replay validates consistency (no duplicate accepted
   asset ids, no seal mismatches) but never modifies the underlying records.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from aigineering.core.provenance import verify_asset_seal
from aigineering.core.integrity import check_store_integrity
from aigineering.core.session import SessionStore, session_trace_path
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import JsonLStore
from aigineering.core.trace import JsonLTraceStore, trace_effective_payload
from aigineering.protocol.types import Session, TraceEntry


def replay_session(
    session_id: str,
    sessions_dir: str = ".aig/sessions",
    traces_dir: str = ".aig/traces",
    store_dir: str = ".aig/store",
    sqlite_path: str = ".aig/store.db",
) -> dict:
    """Read session manifest → load trace → validate consistency.

    Returns dict with keys:
      session, entries, accepted_count, rejected_count, consistent (bool).
    """
    # 1. Load Session from SessionStore
    try:
        session_store = SessionStore(sessions_dir=sessions_dir)
        session: Optional[Session] = session_store.get_session(session_id)
    except Exception as error:
        return _replay_error(None, "Unable to read session data", type(error).__name__)
    if session is None:
        return _replay_error(
            None, f"Session '{session_id}' not found in {sessions_dir}"
        )

    # 2. Find the matching trace file
    trace_dir = Path(traces_dir)
    trace_store: Optional[JsonLTraceStore] = None
    selected_trace_path: Path | None = None
    entries: list[TraceEntry] = []

    # Try direct match: traces_dir/{session_id}.jsonl
    try:
        direct_path = session_trace_path(trace_dir, session_id)
    except Exception as error:
        return _replay_error(None, "Invalid session reference", type(error).__name__)
    if direct_path.exists():
        selected_trace_path = direct_path
        try:
            trace_store = JsonLTraceStore(str(direct_path))
            entries = trace_store.get_all()
        except Exception as error:
            return _replay_error(
                session, "Unable to read trace data", type(error).__name__
            )
    else:
        # Search all session_*.jsonl files for one containing the session's trace_ids
        trace_id_set = set(session.trace_ids)
        if trace_dir.exists() and trace_id_set:
            for fp in sorted(
                trace_dir.glob("session_*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                try:
                    candidate_store = JsonLTraceStore(str(fp))
                    candidate_entries = candidate_store.get_all()
                except Exception as error:
                    return _replay_error(
                        session, "Unable to read trace data", type(error).__name__
                    )
                candidate_ids = {e.id for e in candidate_entries}
                # Match if the session's trace_ids are a subset of candidate's entries
                if trace_id_set <= candidate_ids:
                    selected_trace_path = fp
                    trace_store = candidate_store
                    entries = candidate_entries
                    break

    if trace_store is None or not entries:
        return _replay_error(
            session, f"No matching trace file found for session '{session_id}'"
        )

    # 3. Group entries by event_type
    by_event: dict[str, list[TraceEntry]] = {}
    for entry in entries:
        by_event.setdefault(entry.event_type, []).append(entry)

    # 4. Count accepted vs rejected fragments across asset-producing entries
    accepted_count = 0
    rejected_count = 0
    accepted_ids: list[str] = []
    legacy_fragment_scope = ["projection", "tool_executed"]
    candidate_decision_events = {
        "candidate_committed",
        "candidate_rejected",
        "candidate_authentication_rejected",
    }
    committed_candidates = 0
    rejected_candidates = 0
    effect_count = 0
    output_count = 0

    for entry in entries:
        if entry.event_type in legacy_fragment_scope:
            accepted_count += len(entry.accepted_fragments)
            rejected_count += len(entry.rejected_fragments)
            accepted_ids.extend(entry.accepted_fragments)
        if entry.event_type in candidate_decision_events:
            if entry.event_type == "candidate_committed":
                committed_candidates += 1
                output_count += len(entry.accepted_asset_names or ())
                for fragment in entry.accepted_fragments or ():
                    try:
                        descriptor = json.loads(fragment)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(descriptor, dict) and isinstance(
                        descriptor.get("effect_type"), str
                    ):
                        effect_count += 1
            else:
                rejected_candidates += 1

    raw_trace_ids = []
    if selected_trace_path is not None:
        try:
            with selected_trace_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if isinstance(value, dict) and isinstance(value.get("id"), str):
                        raw_trace_ids.append(value["id"])
        except Exception as error:
            return _replay_error(
                session, "Unable to validate trace record IDs", type(error).__name__
            )
    duplicate_trace_ids = sorted(
        trace_id for trace_id in set(raw_trace_ids) if raw_trace_ids.count(trace_id) > 1
    )
    manifest_ids = set(session.trace_ids)
    entry_ids = {entry.id for entry in entries}
    manifest_trace_mismatch = {
        "missing": sorted(manifest_ids - entry_ids),
        "extra": sorted(entry_ids - manifest_ids),
        "duplicate": sorted(
            set(duplicate_trace_ids)
            | {
                trace_id
                for trace_id in manifest_ids
                if session.trace_ids.count(trace_id) > 1
            }
        ),
    }
    has_manifest_trace_mismatch = any(manifest_trace_mismatch.values())

    # 5. Validate: every accepted asset_id appears exactly once across projections
    seen: set[str] = set()
    duplicates: list[str] = []
    for aid in accepted_ids:
        if aid in seen:
            duplicates.append(aid)
        else:
            seen.add(aid)

    seal_mismatches: list[str] = []
    store_path = Path(store_dir)
    assets_path = store_path / "assets.jsonl"
    contracts_path = store_path / "contracts.jsonl"
    if assets_path.exists():
        asset_store = JsonLStore(str(assets_path), str(contracts_path))
        for aid in sorted(seen):
            asset = asset_store.get_asset(aid)
            if asset is not None and not verify_asset_seal(asset):
                seal_mismatches.append(aid)

    sqlite_integrity: dict | None = None
    trace_mismatches: list[str] = []
    missing_asset_ids: list[str] = []
    trace_provenance: dict[str, dict[str, object]] = {}
    not_checked: list[str] = []
    sqlite_file = Path(sqlite_path)
    if sqlite_file.is_file():
        sqlite_store = None
        try:
            sqlite_store = SQLiteStore.snapshot_from_path(sqlite_file)
            sqlite_integrity = check_store_integrity(sqlite_store)
            not_checked.extend(sqlite_integrity.get("not_checked", ()))
            not_checked.extend(sqlite_integrity["not_checked"])
            durable_by_id = {entry.id: entry for entry in sqlite_store.get_all()}
            entries_by_id = {entry.id: entry for entry in entries}
            for revision, record in sqlite_store.scan_runtime_records(
                record_type="trace.recorded"
            ):
                trace_payload = record.payload.get("trace")
                trace_id = (
                    trace_payload.get("id")
                    if isinstance(trace_payload, Mapping)
                    else None
                )
                if isinstance(trace_id, str) and trace_id in session.trace_ids:
                    trace_provenance[trace_id] = {
                        "recorded_at": record.recorded_at,
                        "revision": revision,
                    }
            missing_asset_ids = sorted(
                aid for aid in seen if sqlite_store.get_asset(aid) is None
            )
            for trace_id in session.trace_ids:
                durable = durable_by_id.get(trace_id)
                exported = entries_by_id.get(trace_id)
                if durable is None or exported is None:
                    trace_mismatches.append(trace_id)
                elif trace_effective_payload(durable) != trace_effective_payload(
                    exported
                ):
                    trace_mismatches.append(trace_id)
        except Exception as error:
            sqlite_integrity = {
                "status": "failed",
                "consistent": False,
                "scope": "local_runtime_record_and_materialization_consistency",
                "checked": [],
                "not_checked": [
                    "sqlite_store_unavailable",
                    "session_manifest_authenticity",
                ],
                "error_type": type(error).__name__,
            }
            not_checked.append("session_manifest_authenticity")
        finally:
            if sqlite_store is not None:
                sqlite_store.close()
    else:
        not_checked.extend(
            [
                "sqlite_runtime_records_and_materializations",
                "session_manifest_authenticity",
            ]
        )
    consistent = (
        len(duplicates) == 0
        and len(seal_mismatches) == 0
        and sqlite_integrity is not None
        and sqlite_integrity.get("consistent") is True
        and not trace_mismatches
        and not missing_asset_ids
        and not has_manifest_trace_mismatch
    )

    return {
        "session": session,
        "entries": entries,
        "by_event": by_event,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "legacy_fragment_count_scope": legacy_fragment_scope,
        "candidate_decision_count": committed_candidates + rejected_candidates,
        "accepted_candidate_count": committed_candidates,
        "rejected_candidate_count": rejected_candidates,
        "effect_count": effect_count,
        "accepted_output_count": output_count,
        "manifest_trace_mismatch": (
            manifest_trace_mismatch if has_manifest_trace_mismatch else None
        ),
        "consistent": consistent,
        "duplicate_ids": duplicates if duplicates else None,
        "seal_mismatches": (seal_mismatches if seal_mismatches else None),
        "integrity": sqlite_integrity,
        "trace_mismatches": trace_mismatches or None,
        "missing_asset_ids": missing_asset_ids or None,
        "trace_provenance": trace_provenance,
        "not_checked": not_checked,
    }


def _replay_error(
    session: Session | None, error: str, error_type: str | None = None
) -> dict:
    """Keep replay's read-model response shape stable on failure."""
    return {
        "session": session,
        "entries": [],
        "by_event": {},
        "accepted_count": 0,
        "rejected_count": 0,
        "consistent": False,
        "duplicate_ids": None,
        "seal_mismatches": None,
        "integrity": None,
        "trace_mismatches": None,
        "not_checked": ["sqlite_runtime_records_and_materializations"],
        "error": error,
        "error_type": error_type,
    }


def replay_all(
    sessions_dir: str = ".aig/sessions",
    traces_dir: str = ".aig/traces",
    store_dir: str = ".aig/store",
    sqlite_path: str = ".aig/store.db",
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
            sqlite_path=sqlite_path,
        )
        results.append(result)
    return results
