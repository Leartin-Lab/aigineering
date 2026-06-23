"""Session manifest store — one JSON file per session in .aig/sessions.

.. admonition:: READ MODEL

   Session manifests are **READ MODELS** — projections over contracts, assets,
   and trace.  They are **NOT** authoritative lifecycle state.  Deleting or
   rebuilding a session manifest does not change runtime facts.  The canonical
   truth lives in the append-only trace (``trace.py``) and the durable store
   (``StoreProtocol``).  The session manifest is a convenience snapshot
   rebuilt on demand from those sources.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from aigineering.core.ids import now_iso
from aigineering.protocol.types import Session
from aigineering.protocol.wire import session_from_dict, session_to_dict


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
