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
from aigineering.redis_collaboration import (
    DEFAULT_REDIS_URL,
    RedisCollaborationService,
    RedisCollaborationUnavailable,
)


def _effective_redis_url(redis_url: str | None = None) -> str:
    return redis_url or os.getenv("AIGINEERING_REDIS_URL") or DEFAULT_REDIS_URL


def require_redis(redis_url: str | None = None):
    """Connect to the required Redis service with a bounded health check."""
    selected_url = _effective_redis_url(redis_url)
    try:
        from aigineering.adapters.redis_query import redis_client, redis_errors

        errors = redis_errors()
    except (ImportError, RuntimeError, TypeError, ValueError) as exc:
        raise RedisCollaborationUnavailable(
            "Redis collaboration service unavailable; check Redis installation and health"
        ) from exc
    try:
        client = redis_client(selected_url)
        if not client.ping():
            raise RedisCollaborationUnavailable(
                "Redis collaboration service unavailable; check Redis health"
            )
        return client
    except RedisCollaborationUnavailable:
        raise
    except errors as exc:
        # Never include redis-py exception text: it may echo connection details.
        raise RedisCollaborationUnavailable(
            "Redis collaboration service unavailable; check Redis installation and health"
        ) from exc
    except (RuntimeError, TypeError, ValueError) as exc:
        raise RedisCollaborationUnavailable(
            "Redis collaboration service unavailable; check Redis installation and health"
        ) from exc


def collaboration_service(store, *, redis_url: str | None = None):
    """Construct the required Redis collaboration service for an initialized domain."""
    from aigineering.adapters.redis_query import redis_errors
    from aigineering.core.domain import load_genesis

    client = require_redis(redis_url)
    try:
        genesis = load_genesis(store)
        domain_id = genesis.id
    except (AttributeError, LookupError):
        domain_id = None
    return RedisCollaborationService(
        store,
        client,
        domain_id=domain_id,
        redis_errors=redis_errors(),
    )


def persistent_store(db_path: str = ".aig/store.db") -> SQLiteStore:
    """Open the default local operational StorePort."""
    return SQLiteStore(db_path=db_path)


def query_projection(store, *, redis_url: str | None = None, strict: bool = True):
    """Build the Redis-backed query view; strict is required at application entry."""
    from aigineering.adapters.redis_query import (
        RedisQueryProjection,
        redis_client,
        redis_errors,
    )
    from aigineering.core.domain import load_genesis

    effective_url = _effective_redis_url(redis_url)
    client = require_redis(effective_url) if strict else redis_client(effective_url)
    try:
        genesis = load_genesis(store)
    except (AttributeError, LookupError):
        if not strict:
            return StoreQueryProjection(
                store,
                redis_configured=True,
                reason="domain_uninitialized",
            )
        raise RedisCollaborationUnavailable(
            "Redis collaboration domain_uninitialized; run 'aig domain init'"
        )
    return RedisQueryProjection(
        store,
        client,
        domain_id=genesis.id,
        redis_errors=redis_errors(),
        strict=strict,
    )


def build_worker(
    worker_kind: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
    max_retries: int = 3,
    max_output_tokens: int = 2048,
    thinking_mode: str | None = None,
    response_format: str | None = None,
    capabilities: frozenset[str] | None = None,
    tool_definitions: list[dict[str, object]] | None = None,
    routing_capabilities: frozenset[str] | None = None,
    worker_id: str | None = None,
) -> MockWorker | LLMWorker:
    """Build one configured local worker at the application boundary."""
    if worker_id is not None:
        validate_worker_id(worker_id)
    if worker_kind == "mock":
        return (
            MockWorker(worker_id=worker_id) if worker_id is not None else MockWorker()
        )
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
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            thinking_mode=thinking_mode,
            response_format=response_format,
            capabilities=capabilities or frozenset(),
            tool_definitions=tool_definitions,
            routing_capabilities=routing_capabilities,
            worker_id=worker_id,
        )
    raise ValueError(f"unsupported worker: {worker_kind}")


def validate_worker_id(worker_id: str) -> None:
    """Validate an explicit local execution identity before provisioning it."""
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker ID must be non-empty text")
    try:
        encoded = worker_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("worker ID must be valid UTF-8") from exc
    if len(encoded) > 256:
        raise ValueError("worker ID exceeds 256 UTF-8 bytes")


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
