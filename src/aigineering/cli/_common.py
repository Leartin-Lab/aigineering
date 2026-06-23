"""Shared utility functions for the aig CLI (internal module)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import click

from aigineering.core.engine import Engine
from aigineering.core.ids import (
    hash_asset_content,
    hash_asset_definition,
    hash_contract,
)
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.method_handlers.fail import FailMethodHandler
from aigineering.core.method_handlers.plan import PlanMethodHandler
from aigineering.core.method_handlers.replan import ReplanMethodHandler
from aigineering.core.method_handlers.retry import RetryMethodHandler
from aigineering.core.method_handlers.tool import ToolMethodHandler
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.session import SessionStore
from aigineering.core.store import MemoryStore, StoreProtocol
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import (
    JsonLTraceStore,
    MemoryTraceStore,
    TraceStoreProtocol,
    create_entry,
)
from aigineering.agent.llm import LLMWorker
from aigineering.agent.mock import MockWorker
from aigineering.protocol.types import Asset, Contract, TraceEntry


def _get_trace_dir() -> Path:
    """Return the trace directory (created lazily on first write)."""
    return Path(".aig/traces")


def _get_store_dir() -> Path:
    """Return the persistent asset/contract store directory."""
    return Path(".aig/store")


def _persistent_store() -> SQLiteStore:
    """Create the default local persistent store (SQLite-backed)."""
    return SQLiteStore(db_path=".aig/store.db")


def _default_method_registry() -> MethodRegistry:
    """Return the standard method-first runtime registry for CLI execution."""
    registry = MethodRegistry()
    registry.register("plan", PlanMethodHandler())
    registry.register("replan", ReplanMethodHandler())
    registry.register("retry", RetryMethodHandler())
    registry.register("tool", ToolMethodHandler())
    registry.register("fail", FailMethodHandler())
    return registry


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
        return rf[1:end], rf[end + 1 :].strip()
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
    save_config: bool = False,
    timeout: float = 60.0,
    max_retries: int = 3,
    capabilities: frozenset[str] | None = None,
    behavior_labels: tuple[str, ...] = (),
) -> tuple[StoreProtocol, TraceStoreProtocol, Contract]:
    """Run the build_report hallucination containment demo."""
    if store is None:
        store = MemoryStore()
    if trace_store is None:
        trace_store = MemoryTraceStore()

    ingress = RuntimeIngress(store, trace_store)

    worker = _build_worker(
        worker_kind,
        model,
        base_url,
        timeout=timeout,
        max_retries=max_retries,
        capabilities=capabilities,
    )
    if isinstance(worker, MockWorker):
        raw_output = (
            f"final_report: Report content for goal '{goal}'\n"
            f"citation_summary: Citation summary for goal '{goal}'"
        )
        worker.set_output("build_report", raw_output)

    if save_config and worker_kind == "llm" and model:
        from aigineering.core.capability_descriptors import (
            create_provider_config_snapshot,
        )

        config_asset = create_provider_config_snapshot(
            provider_name="openai",
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            capabilities=tuple(capabilities or ()),
        )
        if ingress is not None:
            ingress.accept_asset(config_asset, source="provider_config")
        else:
            store.add_asset(config_asset)
        if hasattr(trace_store, "append"):
            trace_store.append(
                create_entry(
                    contract_id="control_plane",
                    event_type="asset_injected",
                    parent_id=config_asset.id,
                    relation_type="provider_config",
                    relation_target=config_asset.name,
                    accepted_fragments=[
                        json.dumps(
                            {
                                "asset_id": config_asset.id,
                                "origin": config_asset.origin,
                                "trust_tier": config_asset.trust_tier,
                            },
                            sort_keys=True,
                        )
                    ],
                )
            )

    data_file = Asset(
        id=hash_asset_content("data_file", "Sample data for report generation"),
        name="data_file",
        content="Sample data for report generation",
        definition_hash=hash_asset_definition("data_file"),
        content_hash=hash_asset_content(
            "data_file", "Sample data for report generation"
        ),
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
            labels=list(behavior_labels),
            origin="human",
        ),
        name="build_report",
        inputs=["data_file", "citation_db"],
        outputs=["final_report"],
        activation="data_file AND citation_db",
        budget=5,
    )

    engine = Engine(
        store,
        worker,
        trace_store,
        method_registry=_default_method_registry(),
        ingress=ingress,
    )
    engine.add_contract(contract)
    engine.add_asset(data_file)
    engine.add_asset(citation_db)
    engine.run()

    return store, trace_store, contract


def _build_worker(
    worker_kind: str,
    model: Optional[str],
    base_url: str,
    timeout: float = 60.0,
    max_retries: int = 3,
    capabilities: frozenset[str] | None = None,
) -> MockWorker | LLMWorker:
    if worker_kind == "mock":
        return MockWorker()
    if worker_kind == "llm":
        if not model:
            raise click.ClickException("--model is required when --worker llm")
        return LLMWorker(
            model=model,
            base_url=base_url,
            timeout=int(timeout),
            max_retries=max_retries,
            capabilities=capabilities or frozenset(),
        )
    raise click.ClickException(f"unsupported worker: {worker_kind}")


def _redact_sealed(data: dict) -> dict:
    """Return a copy of *data* with sealed fields redacted.

    Removes ``config_snapshot`` and ``worker_snapshot`` entirely so API keys
    are never leaked into JSON output.
    """
    return {
        k: v for k, v in data.items() if k not in ("config_snapshot", "worker_snapshot")
    }


def _output_json(payload: object) -> None:
    """Write *payload* as indented JSON to stdout."""
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _get_idempotency_path() -> str:
    """Return the path to the idempotency store JSONL file."""
    return str(_get_store_dir() / "idempotency.jsonl")
