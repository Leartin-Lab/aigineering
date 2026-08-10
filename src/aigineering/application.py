"""Neutral local-runtime composition shared by CLI and optional API server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from aigineering.agent.llm import LLMWorker
from aigineering.agent.mock import MockWorker
from aigineering.plugins import (
    default_completion_registry as default_completion_registry,
)
from aigineering.core.session import SessionStore, session_trace_path
from aigineering.core.query_projection import StoreQueryProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import TraceEntry


def persistent_store(db_path: str = ".aig/store.db") -> SQLiteStore:
    """Open the default local operational StorePort."""
    return SQLiteStore(db_path=db_path)


def query_projection(store, *, redis_url: str | None = None):
    """Build the optional disposable read projection for one Store."""
    effective_url = (
        redis_url if redis_url is not None else os.getenv("AIGINEERING_REDIS_URL", "")
    )
    if not effective_url:
        return StoreQueryProjection(store)

    from aigineering.adapters.redis_query import RedisQueryProjection
    from aigineering.core.domain import load_genesis

    try:
        genesis = load_genesis(store)
    except (AttributeError, LookupError):
        return StoreQueryProjection(
            store,
            redis_configured=True,
            reason="domain_uninitialized",
        )
    return RedisQueryProjection.from_url(
        store,
        domain_id=genesis.id,
        redis_url=effective_url,
    )


def build_worker(
    worker_kind: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
    max_retries: int = 3,
    capabilities: frozenset[str] | None = None,
) -> MockWorker | LLMWorker:
    """Build one configured local worker at the application boundary."""
    if worker_kind == "mock":
        return MockWorker()
    if worker_kind == "llm":
        effective_model = model or os.getenv("AIGINEERING_MODEL")
        if not effective_model:
            raise ValueError(
                "LLM execution requires --model or AIGINEERING_MODEL; "
                "use --worker mock only for explicit tests or dry runs"
            )
        return LLMWorker(
            model=effective_model,
            base_url=(
                base_url
                or os.getenv("AIGINEERING_BASE_URL")
                or "https://api.openai.com/v1"
            ),
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
    direct_path = session_trace_path(trace_dir, session_id)
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
