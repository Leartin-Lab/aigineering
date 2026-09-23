"""Session manifest store — one JSON file per session in .aig/sessions.

.. admonition:: READ MODEL

   Session manifests are **READ MODELS** — projections over contracts, assets,
   and trace.  They are **NOT** authoritative lifecycle state.  Deleting or
   rebuilding a session manifest does not change runtime facts.  The canonical
   truth lives in immutable runtime records in the durable Store
   (``StoreProtocol``). Trace stores expose committed trace-record projections;
   the session manifest is a convenience snapshot rebuilt from those facts.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Optional

from aigineering.core.ids import now_iso
from aigineering.protocol.types import Session
from aigineering.protocol.wire import session_from_dict, session_to_dict
from aigineering.protocol.runtime_record import RuntimeRecord


_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def runtime_record_contract_references(
    record: RuntimeRecord,
    records_by_id: Mapping[str, RuntimeRecord] | None = None,
) -> frozenset[str]:
    """Return Contract IDs explicitly referenced by a typed runtime record.

    Candidate commitment traces without a claim binding use the historical
    ``commitment`` label. They are attributed only when the trace's candidate
    ID joins to a committed record whose committed record IDs resolve to a
    declared Contract; the label itself never grants ownership.
    """
    return _runtime_record_contract_references(record, records_by_id or {}, set())


def _runtime_record_contract_references(
    record: RuntimeRecord,
    index: Mapping[str, RuntimeRecord],
    visited: set[str],
) -> frozenset[str]:
    if record.id in visited:
        return frozenset()
    visited.add(record.id)
    payload = record.payload
    references: set[str] = set()
    for key in ("contract_id", "source_contract_id", "parent_contract_id"):
        value = payload.get(key)
        if isinstance(value, str):
            references.add(value)

    if record.record_type == "contract.declared":
        contract = payload.get("contract")
        if isinstance(contract, Mapping):
            for key in ("id", "parent_id"):
                value = contract.get(key)
                if isinstance(value, str):
                    references.add(value)
    elif record.record_type == "asset.committed":
        asset = payload.get("asset")
        if isinstance(asset, Mapping):
            creator = asset.get("created_by")
            if isinstance(creator, str):
                references.add(creator)
    elif record.record_type == "trace.recorded":
        trace = payload.get("trace")
        if isinstance(trace, Mapping):
            contract_id = trace.get("contract_id")
            if isinstance(contract_id, str) and contract_id != "commitment":
                references.add(contract_id)
            elif contract_id == "commitment":
                candidate_ids: set[str] = set()
                for fragment in trace.get("accepted_fragments", ()):
                    if not isinstance(fragment, str):
                        continue
                    try:
                        candidate_id = json.loads(fragment).get("candidate_id")
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if isinstance(candidate_id, str):
                        candidate_ids.add(candidate_id)
                for candidate_record in index.values():
                    if (
                        candidate_record.record_type != "candidate.committed"
                        or candidate_record.payload.get("candidate_id")
                        not in candidate_ids
                    ):
                        continue
                    for committed_id in candidate_record.payload.get(
                        "committed_record_ids", ()
                    ):
                        committed = index.get(committed_id)
                        if committed is not None:
                            references.update(
                                _runtime_record_contract_references(
                                    committed, index, visited
                                )
                            )
    elif record.record_type == "candidate.committed":
        for committed_id in payload.get("committed_record_ids", ()):
            committed = index.get(committed_id)
            if committed is not None:
                references.update(
                    _runtime_record_contract_references(committed, index, visited)
                )

    return frozenset(references)


def validate_session_id(session_id: str) -> str:
    """Return a safe local session identifier or fail closed.

    Session identifiers participate in local manifest and trace filenames, so
    path separators, traversal components, and platform-specific alternates
    are never valid protocol data.
    """
    if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("invalid session id")
    return session_id


def session_trace_path(traces_dir: str | Path, session_id: str) -> Path:
    """Resolve one validated session trace below *traces_dir*."""
    return Path(traces_dir) / f"{validate_session_id(session_id)}.jsonl"


class SessionStore:
    """Simple JSON file-based session manifest store.

    Each session is written as ``session_<id>.json`` under the configured
    *sessions_dir* (default ``.aig/sessions``).
    """

    def __init__(self, sessions_dir: str = ".aig/sessions") -> None:
        self._dir = Path(sessions_dir)
        if not self._dir.exists():
            self._dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, session_id: str) -> Path:
        session_id = validate_session_id(session_id)
        filename = (
            f"{session_id}.json"
            if session_id.startswith("session_")
            else f"session_{session_id}.json"
        )
        return self._dir / filename

    def create_session(self, session: Session) -> None:
        """Persist *session* to ``session_<id>.json``."""
        if not session.created_at:
            # Session is frozen — build a new one with the timestamp set.
            session = Session(
                id=session.id,
                root_contract_id=session.root_contract_id,
                contract_ids=session.contract_ids,
                asset_ids=session.asset_ids,
                trace_ids=session.trace_ids,
                config_snapshot=session.config_snapshot,
                worker_snapshot=session.worker_snapshot,
                created_at=now_iso(),
                scope=session.scope,
                execution_start_revision=session.execution_start_revision,
                execution_end_revision=session.execution_end_revision,
                produced_asset_ids=session.produced_asset_ids,
                unassigned_trace_ids=session.unassigned_trace_ids,
            )
        payload = session_to_dict(session)
        with open(self._file_path(session.id), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return the session with *session_id*, or *None*."""
        path = self._file_path(session_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return session_from_dict(data)

    def list_sessions(self) -> list[Session]:
        """Return all persisted sessions, most-recent first."""
        sessions: list[Session] = []
        if not self._dir.exists():
            return sessions
        for fp in sorted(
            self._dir.glob("session_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions.append(session_from_dict(data))
        return sessions
