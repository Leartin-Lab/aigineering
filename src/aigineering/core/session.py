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
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional

from aigineering.core.ids import now_iso
from aigineering.protocol.types import Session
from aigineering.protocol.wire import session_from_dict, session_to_dict
from aigineering.protocol.runtime_record import RuntimeRecord


_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


@dataclass(frozen=True)
class RuntimeRecordReferenceContext:
    """One capture-local index for typed Candidate commitment heads."""

    records_by_id: Mapping[str, RuntimeRecord]
    candidate_heads_by_id: Mapping[str, tuple[tuple[int, RuntimeRecord], ...]]
    fallback_to_legacy_commitment_scan: bool


def build_runtime_record_reference_context(
    records_by_id: Mapping[str, RuntimeRecord],
) -> RuntimeRecordReferenceContext:
    """Index exact-string Candidate IDs in the effective mapping's value order."""
    heads: dict[str, list[tuple[int, RuntimeRecord]]] = {}
    fallback = False
    for position, record in enumerate(records_by_id.values()):
        if record.record_type != "candidate.committed":
            continue
        candidate_id = record.payload.get("candidate_id")
        if type(candidate_id) is not str:
            fallback = True
            continue
        heads.setdefault(candidate_id, []).append((position, record))
    frozen_heads = MappingProxyType(
        {candidate_id: tuple(bucket) for candidate_id, bucket in heads.items()}
    )
    return RuntimeRecordReferenceContext(records_by_id, frozen_heads, fallback)


def runtime_record_contract_references(
    record: RuntimeRecord,
    records_by_id: Mapping[str, RuntimeRecord]
    | RuntimeRecordReferenceContext
    | None = None,
) -> frozenset[str]:
    """Return Contract IDs explicitly referenced by a typed runtime record.

    Candidate commitment traces without a claim binding use the historical
    ``commitment`` label. They are attributed only when the trace's candidate
    ID joins to a committed record whose committed record IDs resolve to a
    declared Contract; the label itself never grants ownership.
    """
    context = (
        records_by_id
        if isinstance(records_by_id, RuntimeRecordReferenceContext)
        else None
    )
    index = context.records_by_id if context is not None else records_by_id or {}
    return _runtime_record_contract_references(record, index, set(), context)


def runtime_record_worker_references(
    record: RuntimeRecord,
    records_by_id: Mapping[str, RuntimeRecord] | None = None,
) -> frozenset[str]:
    """Return Worker IDs explicitly named by known typed runtime facts.

    This is a scoped read-model helper. It never searches descriptions, trace
    prose, or arbitrary nested candidate content for strings that resemble IDs.
    """
    return _runtime_record_worker_references(record, records_by_id or {}, set())


def _runtime_record_worker_references(
    record: RuntimeRecord,
    index: Mapping[str, RuntimeRecord],
    visited: set[str],
) -> frozenset[str]:
    if record.id in visited:
        return frozenset()
    visited.add(record.id)
    typed_worker_records = {
        "worker.registered",
        "worker.claim.requested",
        "worker.claim.renew.requested",
        "claim.granted",
        "claim.renewed",
        "claim.expired",
        "claim.released",
        "claim.submitted",
        "claim.assignment.changed",
    }
    references: set[str] = set()
    if record.record_type in typed_worker_records:
        value = record.payload.get("worker_id")
        if isinstance(value, str) and value:
            references.add(value)
    if record.record_type == "candidate.committed":
        for committed_id in record.payload.get("committed_record_ids", ()):
            committed = index.get(committed_id)
            if committed is not None:
                references.update(
                    _runtime_record_worker_references(committed, index, visited)
                )
    elif record.record_type in {"claim.renewed", "worker.claim.renew.requested"}:
        for parent_id in record.causal_parents:
            parent = index.get(parent_id)
            if parent is not None and parent.record_type == "claim.granted":
                references.update(
                    _runtime_record_worker_references(parent, index, visited)
                )
    # Derive successor Worker references from typed causal facts instead of
    # teaching this core reader about plugin-owned handoff/revision records.
    for parent_id in record.causal_parents:
        parent = index.get(parent_id)
        if parent is not None:
            references.update(_runtime_record_worker_references(parent, index, visited))
    return frozenset(references)


def _runtime_record_contract_references(
    record: RuntimeRecord,
    index: Mapping[str, RuntimeRecord],
    visited: set[str],
    context: RuntimeRecordReferenceContext | None = None,
) -> frozenset[str]:
    if record.id in visited:
        return frozenset()
    visited.add(record.id)
    payload = record.payload
    references: set[str] = set()
    reference_fields = (
        "contract_id",
        "source_contract_id",
        "from_contract_id",
        "parent_contract_id",
        "child_contract_id",
        "successor_id",
    )
    if record.record_type in {
        "claim.constraint.placed",
        "claim.constraint.released",
    }:
        reference_fields += ("prerequisite_contract_id",)
    for key in reference_fields:
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
                if (
                    context is not None
                    and not context.fallback_to_legacy_commitment_scan
                ):
                    ordered_heads = sorted(
                        (
                            head_entry
                            for candidate_id in candidate_ids
                            for head_entry in context.candidate_heads_by_id.get(
                                candidate_id, ()
                            )
                        ),
                        key=lambda head_entry: head_entry[0],
                    )
                    selected_heads = (head for _position, head in ordered_heads)
                else:
                    selected_heads = (
                        candidate_record
                        for candidate_record in index.values()
                        if (
                            candidate_record.record_type == "candidate.committed"
                            and candidate_record.payload.get("candidate_id")
                            in candidate_ids
                        )
                    )
                for candidate_record in selected_heads:
                    for committed_id in candidate_record.payload.get(
                        "committed_record_ids", ()
                    ):
                        committed = index.get(committed_id)
                        if committed is not None:
                            references.update(
                                _runtime_record_contract_references(
                                    committed, index, visited, context
                                )
                            )
    elif record.record_type == "candidate.committed":
        for committed_id in payload.get("committed_record_ids", ()):
            committed = index.get(committed_id)
            if committed is not None:
                references.update(
                    _runtime_record_contract_references(
                        committed, index, visited, context
                    )
                )
    elif record.record_type in {"claim.renewed", "worker.claim.renew.requested"}:
        for parent_id in record.causal_parents:
            parent = index.get(parent_id)
            if parent is not None and parent.record_type == "claim.granted":
                references.update(
                    _runtime_record_contract_references(parent, index, visited, context)
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
        destination = self._file_path(session.id)
        self._dir.mkdir(parents=True, exist_ok=True)
        with open(destination, "w", encoding="utf-8") as f:
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
