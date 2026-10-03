"""Shared utility functions for the aig CLI (internal module)."""

from __future__ import annotations

import json
import re
import time
import uuid
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

_QUICK_INPUT_MAX_BYTES = 1_048_576
_QUICK_INPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*\Z")


def _load_quick_goal_inputs(
    goal: str, input_specs: tuple[str, ...], run_key: str
) -> tuple[list[Asset], str]:
    """Read and validate all goal inputs before the caller opens a Store."""
    sources: list[tuple[str, str]] = [("goal", goal)]
    seen: set[str] = set()
    for spec in input_specs:
        source_name, separator, raw_path = spec.partition("=")
        if not separator or not source_name or not raw_path:
            raise click.BadParameter(
                "expected NAME=PATH with a non-empty asset name and file path",
                param_hint="--input",
            )
        if _QUICK_INPUT_NAME.fullmatch(source_name) is None:
            raise click.BadParameter(
                f"invalid input name {source_name!r}; use letters, digits, '_', '.', ':' or '-'",
                param_hint="--input",
            )
        if source_name in seen:
            raise click.BadParameter(
                f"duplicate input name {source_name!r}", param_hint="--input"
            )
        seen.add(source_name)
        path = Path(raw_path)
        if not path.is_file():
            raise click.BadParameter(
                f"input file is not a regular file: {raw_path}", param_hint="--input"
            )
        try:
            with path.open("rb") as stream:
                data = stream.read(_QUICK_INPUT_MAX_BYTES + 1)
        except OSError as exc:
            raise click.BadParameter(
                f"cannot read input file {raw_path}: {exc.strerror or type(exc).__name__}",
                param_hint="--input",
            ) from exc
        if len(data) > _QUICK_INPUT_MAX_BYTES:
            raise click.BadParameter(
                f"input file exceeds {_QUICK_INPUT_MAX_BYTES} bytes: {raw_path}",
                param_hint="--input",
            )
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise click.BadParameter(
                f"input file must be valid UTF-8: {raw_path}", param_hint="--input"
            ) from exc
        if not content.strip():
            raise click.BadParameter(
                f"input file is empty: {raw_path}", param_hint="--input"
            )
        sources.append((source_name, content))

    assets: list[Asset] = []
    slot_sources: list[str] = []
    for index, (source_name, content) in enumerate(sources):
        slot = f"aig_input_{run_key}_{index}"
        content_hash = hash_asset_content(slot, content)
        assets.append(
            Asset(
                id=content_hash,
                name=slot,
                content=content,
                definition_hash=hash_asset_definition(slot),
                content_hash=content_hash,
                origin="human",
                trust_tier="human",
            )
        )
        slot_sources.append(f"{slot} (from {source_name})")
    return assets, ", ".join(slot_sources)


@dataclass(frozen=True)
class _RunSessionScope:
    contract_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    produced_asset_ids: tuple[str, ...]
    trace_ids: tuple[str, ...]
    unassigned_trace_ids: tuple[str, ...]
    start_revision: int
    end_revision: int


def _contract_tree_ids(contracts: list[Contract], root_contract_id: str) -> set[str]:
    """Return the root Contract and all immutable descendants in the Store."""
    contract_ids = (
        {root_contract_id}
        if any(item.id == root_contract_id for item in contracts)
        else set()
    )
    changed = True
    while changed:
        changed = False
        for contract in contracts:
            if contract.parent_id in contract_ids and contract.id not in contract_ids:
                contract_ids.add(contract.id)
                changed = True
    return contract_ids


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
    contract_ids = _contract_tree_ids(list(contracts.values()), root_contract_id)

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


def _claim_next_root_tree_package(
    store,
    *,
    worker_id: str,
    root_contract_id: str,
    candidate_publishers,
):
    """Claim only this quick-run root or one of its current descendants."""
    from aigineering.runtime import claim_next_package

    for contract_id in sorted(
        _contract_tree_ids(store.get_all_contracts(), root_contract_id)
    ):
        claimed = claim_next_package(
            store,
            worker_id=worker_id,
            contract_id=contract_id,
            candidate_publishers=candidate_publishers,
        )
        if claimed is not None:
            return claimed
    return None


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
    max_output_tokens: int = 2048,
    thinking_mode: str | None = None,
    response_format: str | None = None,
    capabilities: frozenset[str] | None = None,
    behavior_labels: tuple[str, ...] = (),
    quick_inputs: list[Asset] | None = None,
    quick_input_description: str | None = None,
    raise_on_incomplete: bool = True,
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
    publication_id = uuid.uuid4().hex
    output_name = f"final_report_{publication_id}"

    try:
        worker = build_worker(
            worker_kind,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            thinking_mode=thinking_mode,
            response_format=response_format,
            capabilities=capabilities,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if isinstance(worker, MockWorker):
        raw_output = f"{output_name}: Report content for goal '{goal}'"
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
            max_output_tokens=max_output_tokens,
            thinking_mode=thinking_mode,
            response_format=response_format,
        )
        require_accepted(
            commit_local_effect(
                store,
                asset_proposal_effect(config_asset),
                idempotency_key=f"demo:{publication_id}:provider-config",
            )
        )

    if quick_inputs is None:
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
        input_assets = [data_file, citation_db]
        input_description = "Sample data and citation assets from `aig demo`."
    else:
        input_assets = quick_inputs
        input_description = quick_input_description or "User-provided goal inputs."

    contract = contract_from_fields(
        name="build_report",
        description=(
            f"Goal: {goal}\nInput slots: {input_description}"
            if quick_inputs is not None
            else f"Build a report for goal: {goal}\n{input_description}"
        ),
        inputs=[asset.name for asset in input_assets],
        outputs=[output_name],
        activation=" AND ".join(asset.name for asset in input_assets),
        budget=5,
        tool_scope=[],
        labels=list(behavior_labels),
        origin="human",
    )

    publications = [("contract", contract_declaration_effect(contract))]
    publications.extend(
        (f"input-{index}", asset_proposal_effect(asset))
        for index, asset in enumerate(input_assets)
    )
    for suffix, effect in publications:
        require_accepted(
            commit_local_effect(
                store,
                effect,
                idempotency_key=f"demo:{publication_id}:{suffix}",
            )
        )

    from aigineering.runtime import (
        WorkerInvocationError,
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
        claimed = _claim_next_root_tree_package(
            store,
            worker_id=host.worker_id,
            root_contract_id=contract.id,
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
    if status["status"] != "completed" and raise_on_incomplete:
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
