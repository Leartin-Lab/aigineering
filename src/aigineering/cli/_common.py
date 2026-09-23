"""Shared utility functions for the aig CLI (internal module)."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click

from aigineering.core.ids import (
    contract_from_fields,
    hash_asset_content,
    hash_asset_definition,
)
from aigineering.core.session import runtime_record_contract_references
from aigineering.application import (
    build_worker,
    default_completion_registry,
    find_trace_for_session,
    latest_session_file,
    persistent_store,
    query_projection,
)
from aigineering.cli._candidate import commit_local_effect, require_accepted
from aigineering.local_identity import ensure_local_domain
from aigineering.plugins import CompletionRegistry
from aigineering.core.store import StoreProtocol
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import require_operational_store
from aigineering.core.trace import (
    JsonLTraceStore,
    TraceStoreProtocol,
)
from aigineering.agent.mock import MockWorker
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Asset, Contract, TraceEntry
from aigineering.protocol.wire import trace_entry_from_dict


@dataclass(frozen=True)
class _RunSessionScope:
    contract_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    produced_asset_ids: tuple[str, ...]
    trace_ids: tuple[str, ...]
    unassigned_trace_ids: tuple[str, ...]
    start_revision: int
    end_revision: int


def _session_scope_for_run(
    store: StoreProtocol,
    trace_store: TraceStoreProtocol,
    root_contract_id: str,
    start_revision: int,
    end_revision: int,
) -> _RunSessionScope:
    """Export and describe only this run's root tree and referenced evidence."""
    records = store.scan_runtime_records(
        after_revision=start_revision, through_revision=end_revision
    )
    contracts = {item.id: item for item in store.get_all_contracts()}
    contract_ids = {root_contract_id} if root_contract_id in contracts else set()
    changed = True
    while changed:
        changed = False
        for contract in contracts.values():
            if contract.parent_id in contract_ids and contract.id not in contract_ids:
                contract_ids.add(contract.id)
                changed = True

    records_by_id = {record.id: record for _, record in records}

    assets: set[str] = set()
    produced_assets: set[str] = set()
    for contract_id in contract_ids:
        contract = contracts[contract_id]
        assets.update(
            asset_id
            for asset_id in contract.context_asset_ids
            if store.get_asset(asset_id) is not None
        )

    for _, record in records:
        if record.record_type != "asset.committed":
            continue
        raw_asset = record.payload.get("asset")
        if not isinstance(raw_asset, Mapping):
            continue
        asset_id = raw_asset.get("id")
        if raw_asset.get("created_by") in contract_ids and isinstance(asset_id, str):
            if store.get_asset(asset_id) is not None:
                produced_assets.add(asset_id)
                assets.add(asset_id)

    trace_ids: list[str] = []
    unassigned_trace_ids: list[str] = []
    for _, record in records:
        if record.record_type != "trace.recorded":
            continue
        raw_trace = record.payload.get("trace")
        if not isinstance(raw_trace, Mapping):
            continue
        entry = trace_entry_from_dict(dict(raw_trace))
        references = runtime_record_contract_references(record, records_by_id)
        belongs_to_tree = bool(references & contract_ids)
        if not belongs_to_tree:
            if entry.contract_id == "commitment" and not references:
                unassigned_trace_ids.append(entry.id)
            continue
        trace_store.append(entry)
        trace_ids.append(entry.id)
        for asset_id in (*entry.disclosed_assets, *entry.accepted_fragments):
            if store.get_asset(asset_id) is not None:
                assets.add(asset_id)

    return _RunSessionScope(
        contract_ids=tuple(sorted(contract_ids)),
        asset_ids=tuple(sorted(assets)),
        produced_asset_ids=tuple(sorted(produced_assets)),
        trace_ids=tuple(trace_ids),
        unassigned_trace_ids=tuple(unassigned_trace_ids),
        start_revision=start_revision,
        end_revision=end_revision,
    )


def _get_trace_dir() -> Path:
    """Return the trace directory (created lazily on first write)."""
    return Path(".aig/traces")


def _get_store_dir() -> Path:
    """Return the persistent asset/contract store directory."""
    return Path(".aig/store")


def _persistent_store() -> SQLiteStore:
    """Create the default local persistent store (SQLite-backed)."""
    return persistent_store()


def _persistent_snapshot(db_path: str | Path = ".aig/store.db") -> SQLiteStore:
    """Open a read-only snapshot of the configured local SQLite Store."""
    return SQLiteStore.snapshot_from_path(db_path)


def _query_projection(store: StoreProtocol):
    """Return the configured read-only query projection."""
    return query_projection(store)


def _default_completion_registry() -> CompletionRegistry:
    """Return the transitional completion registry for CLI execution."""
    return default_completion_registry()


def _session_id() -> str:
    """Return a nanosecond-timestamp session identifier to avoid same-second collisions."""
    return f"session_{time.time_ns()}"


def _latest_session_file() -> Optional[Path]:
    """Return the newest session_*.jsonl file in the trace dir, or None."""
    return latest_session_file(str(_get_trace_dir()))


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
    return find_trace_for_session(session_id, sessions_dir, traces_dir)


def _run_demo(
    goal: str,
    trace_store: TraceStoreProtocol | None = None,
    store: StoreProtocol | None = None,
    worker_kind: str = "llm",
    model: Optional[str] = None,
    base_url: str = "https://api.openai.com/v1",
    save_config: bool = False,
    timeout: float = 60.0,
    max_retries: int = 3,
    capabilities: frozenset[str] | None = None,
    behavior_labels: tuple[str, ...] = (),
) -> tuple[StoreProtocol, TraceStoreProtocol, Contract, _RunSessionScope]:
    """Run a bounded quick-goal task graph through the normal Worker pool."""
    if store is None:
        store = SQLiteStore(":memory:")
    store = require_operational_store(store)
    if trace_store is None:
        trace_store = store

    start_revision = store.get_runtime_revision()

    try:
        ensure_local_domain(store)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    publication_id = time.time_ns()

    try:
        worker = build_worker(
            worker_kind,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            capabilities=capabilities,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if isinstance(worker, MockWorker):
        raw_output = f"final_report: Report content for goal '{goal}'"
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
        require_accepted(
            commit_local_effect(
                store,
                asset_proposal_effect(config_asset),
                idempotency_key=f"demo:{publication_id}:provider-config",
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

    contract = contract_from_fields(
        name="build_report",
        description=f"Build a report for goal: {goal}",
        inputs=["data_file", "citation_db"],
        outputs=["final_report"],
        activation="data_file AND citation_db",
        budget=5,
        tool_scope=[],
        labels=list(behavior_labels),
        origin="human",
    )

    for suffix, effect in (
        ("contract", contract_declaration_effect(contract)),
        ("data-file", asset_proposal_effect(data_file)),
        ("citation-db", asset_proposal_effect(citation_db)),
    ):
        require_accepted(
            commit_local_effect(
                store,
                effect,
                idempotency_key=f"demo:{publication_id}:{suffix}",
            )
        )

    from aigineering.runtime import (
        WorkerInvocationError,
        claim_next_package,
        execute_claimed_package,
        process_task_completions,
        run_runtime_maintenance_step,
    )
    from aigineering.local_identity import (
        ensure_local_runtime_publishers,
        ensure_local_worker_host,
    )
    from aigineering.cli.task_state import project_task_status

    host = ensure_local_worker_host(store, worker)
    publishers = ensure_local_runtime_publishers(store)
    registry = default_completion_registry()
    last_submission = "none"
    for _ in range(64):
        run_runtime_maintenance_step(store, registry, candidate_publishers=publishers)
        status = project_task_status(contract, store)
        if status["terminal"]:
            break
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            candidate_publishers=publishers,
        )
        if claimed is None:
            break
        try:
            result = execute_claimed_package(
                claimed,
                host,
                store,
                trace_store,
                candidate_publishers=publishers,
            )
        except WorkerInvocationError:
            last_submission = "worker_failed"
            continue
        last_submission = str(result.get("status", "unknown"))
    process_task_completions(store, registry, candidate_publishers=publishers)
    status = project_task_status(contract, store)
    if status["status"] != "completed":
        raise RuntimeError(
            "demo worker did not complete declared outputs: "
            f"submission={last_submission} "
            f"task={status['status']} "
            f"risks={status.get('silent_failure_risks', [])}"
        )

    # Export from a stable, bounded runtime-record revision window. Contract
    # ownership determines root-tree membership; global trace counts do not.
    snapshot_factory = getattr(store, "snapshot", None)
    scope_store = snapshot_factory() if callable(snapshot_factory) else store
    try:
        end_revision = scope_store.get_runtime_revision()
        scope = _session_scope_for_run(
            scope_store, trace_store, contract.id, start_revision, end_revision
        )
    finally:
        if scope_store is not store:
            scope_store.close()
    return store, trace_store, contract, scope


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
