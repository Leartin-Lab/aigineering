"""Neutral local-runtime composition shared by CLI and optional API server."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aigineering.agent.llm import LLMWorker
from aigineering.agent.mock import MockWorker
from aigineering.plugins import (
    default_completion_registry as default_completion_registry,
)
from aigineering.core.session import SessionStore
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import TraceEntry


def persistent_store(db_path: str = ".aig/store.db") -> SQLiteStore:
    """Open the default local operational StorePort."""
    return SQLiteStore(db_path=db_path)


def build_worker(
    worker_kind: str,
    *,
    model: str | None = None,
    base_url: str = "https://api.openai.com/v1",
    timeout: float = 60.0,
    max_retries: int = 3,
    capabilities: frozenset[str] | None = None,
) -> MockWorker | LLMWorker:
    """Build one configured local worker at the application boundary."""
    if worker_kind == "mock":
        return MockWorker()
    if worker_kind == "llm":
        if not model:
            raise ValueError("--model is required when --worker llm")
        return LLMWorker(
            model=model,
            base_url=base_url,
            timeout=int(timeout),
            max_retries=max_retries,
            capabilities=capabilities or frozenset(),
        )
    raise ValueError(f"unsupported worker: {worker_kind}")


def latest_session_file(traces_dir: str = ".aig/traces") -> Optional[Path]:
    """Return the newest local JSONL audit export, if one exists."""
    trace_dir = Path(traces_dir)
    if not trace_dir.exists():
        return None
    files = sorted(
        trace_dir.glob("session_*.jsonl"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    return files[0] if files else None


def find_trace_for_session(
    session_id: str,
    sessions_dir: str = ".aig/sessions",
    traces_dir: str = ".aig/traces",
) -> tuple[Optional[JsonLTraceStore], Optional[list[TraceEntry]]]:
    """Resolve a session manifest to its matching local JSONL audit export."""
    session = SessionStore(sessions_dir=sessions_dir).get_session(session_id)
    if session is None:
        return None, None

    trace_dir = Path(traces_dir)
    trace_ids = set(session.trace_ids)
    direct_path = trace_dir / f"{session_id}.jsonl"
    if direct_path.exists():
        store = JsonLTraceStore(str(direct_path))
        return store, store.get_all()

    if trace_dir.exists() and trace_ids:
        files = sorted(
            trace_dir.glob("session_*.jsonl"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in files:
            candidate = JsonLTraceStore(str(path))
            candidate_ids = {entry.id for entry in candidate.get_all()}
            if trace_ids <= candidate_ids or trace_ids & candidate_ids:
                return candidate, candidate.get_all()
    return None, None
