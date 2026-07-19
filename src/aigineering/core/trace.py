"""Append-only TraceStore — the runtime record.

.. admonition:: IMMUTABLE TERMINAL EVENTS

   Terminal lifecycle events — ``"complete"``, ``"failed"``, ``"cancelled"``,
   ``"unreachable"`` — are **immutable**.  Once appended, they must never be
   deleted, modified, or duplicated.  Consumers (replay, state serialization,
   CLI views) rely on every terminal event being present exactly once for a
   given contract.  Idempotency guards in ``Engine._emit_terminal_event()``
   enforce this at the emission boundary.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from aigineering.core.ids import now_iso, hash_event
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import TraceEntry
from aigineering.protocol.wire import trace_entry_from_dict, trace_entry_to_dict

_logger = logging.getLogger(__name__)


def create_entry(
    contract_id: str,
    event_type: str,
    *,
    parent_id: Optional[str] = None,
    sequence: int = 0,
    disclosed_assets: Optional[list[str]] = None,
    worker_id: Optional[str] = None,
    candidate_raw: Optional[str] = None,
    accepted_fragments: Optional[list[str]] = None,
    accepted_asset_names: Optional[list[str]] = None,
    rejected_fragments: Optional[list[str]] = None,
    authority_policy: Optional[str] = None,
    authority_result: Optional[str] = None,
    budget_remaining: int = 0,
    relation_type: Optional[str] = None,
    relation_target: Optional[str] = None,
    usage_metadata: Optional[Mapping[str, Any]] = None,
) -> TraceEntry:
    effective_payload = {
        "disclosed_assets": list(disclosed_assets or ()),
        "worker_id": worker_id,
        "candidate_raw": candidate_raw,
        "accepted_fragments": list(accepted_fragments or ()),
        "accepted_asset_names": list(accepted_asset_names or ()),
        "rejected_fragments": list(rejected_fragments or ()),
        "authority_policy": authority_policy,
        "authority_result": authority_result,
        "budget_remaining": budget_remaining,
        "relation_type": relation_type,
        "relation_target": relation_target,
        "usage_metadata": deep_thaw(usage_metadata)
        if usage_metadata is not None
        else None,
    }
    entry_id = hash_event(
        contract_id=contract_id,
        event_type=event_type,
        sequence=sequence,
        parent_id=parent_id,
        payload=effective_payload,
    )
    return TraceEntry(
        id=entry_id,
        parent_id=parent_id,
        contract_id=contract_id,
        event_type=event_type,
        disclosed_assets=disclosed_assets if disclosed_assets is not None else [],
        worker_id=worker_id,
        candidate_raw=candidate_raw,
        accepted_fragments=accepted_fragments if accepted_fragments is not None else [],
        accepted_asset_names=accepted_asset_names
        if accepted_asset_names is not None
        else [],
        rejected_fragments=rejected_fragments if rejected_fragments is not None else [],
        authority_policy=authority_policy,
        authority_result=authority_result,
        budget_remaining=budget_remaining,
        relation_type=relation_type,
        relation_target=relation_target,
        timestamp=now_iso(),
        usage_metadata=usage_metadata,
    )


@runtime_checkable
class TraceStoreProtocol(Protocol):
    """Protocol that any trace store (in-memory or persistent) must satisfy."""

    def append(self, entry: TraceEntry) -> None: ...
    def new_entry(
        self, contract_id: str, event_type: str, **kwargs: object
    ) -> TraceEntry: ...
    def get_by_contract(self, contract_id: str) -> list[TraceEntry]: ...
    def get_by_event_type(self, event_type: str) -> list[TraceEntry]: ...
    def get_all(self) -> list[TraceEntry]: ...
    def get_reverse_lineage(self, asset_id: str) -> list[TraceEntry]: ...


class MemoryTraceStore:
    def __init__(self) -> None:
        self.entries: list[TraceEntry] = []
        self._seq: int = 0

    @property
    def sequence(self) -> int:
        return self._seq

    def append(self, entry: TraceEntry) -> None:
        from aigineering.core.record_conflict import ImmutableRecordConflict

        for existing in self.entries:
            if existing.id != entry.id:
                continue
            if trace_effective_payload(existing) == trace_effective_payload(entry):
                return
            raise ImmutableRecordConflict("trace event", entry.id)
        self.entries.append(entry)
        self._seq += 1

    def new_entry(
        self,
        contract_id: str,
        event_type: str,
        **kwargs: object,
    ) -> TraceEntry:
        if kwargs.get("parent_id") is None:
            for e in reversed(self.entries):
                if e.contract_id == contract_id:
                    kwargs["parent_id"] = e.id
                    break
        entry = create_entry(
            contract_id=contract_id,
            event_type=event_type,
            sequence=self._seq,
            **kwargs,  # type: ignore[arg-type]
        )
        self.append(entry)
        return entry

    def get_by_contract(self, contract_id: str) -> list[TraceEntry]:
        return [e for e in self.entries if e.contract_id == contract_id]

    def get_by_event_type(self, event_type: str) -> list[TraceEntry]:
        return [e for e in self.entries if e.event_type == event_type]

    def get_all(self) -> list[TraceEntry]:
        return list(self.entries)

    def get_reverse_lineage(self, asset_id: str) -> list[TraceEntry]:
        return [
            entry for entry in self.entries if _entry_references_asset(entry, asset_id)
        ]


class JsonLTraceStore:
    """Persistent JSONL trace store — one JSON object per line."""

    def __init__(self, file_path: str) -> None:
        self._file_path = file_path
        self._entries: list[TraceEntry] = []
        self._seq: int = 0
        parent = Path(file_path).parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._entries = self._load_existing()
        self._seq = len(self._entries)

    @property
    def sequence(self) -> int:
        return self._seq

    def _load_existing(self) -> list[TraceEntry]:
        if not os.path.exists(self._file_path):
            return []
        entries: list[TraceEntry] = []
        by_id: dict[str, TraceEntry] = {}
        with open(self._file_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    entry = trace_entry_from_dict(json.loads(stripped))
                    existing = by_id.get(entry.id)
                    if existing is not None:
                        if trace_effective_payload(existing) != trace_effective_payload(
                            entry
                        ):
                            from aigineering.core.record_conflict import (
                                ImmutableRecordConflict,
                            )

                            raise ImmutableRecordConflict("trace event", entry.id)
                        continue
                    entries.append(entry)
                    by_id[entry.id] = entry
        return entries

    def _write_line(self, entry: TraceEntry) -> None:
        line = json.dumps(trace_entry_to_dict(entry), ensure_ascii=False) + "\n"
        with open(self._file_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                _logger.warning("fsync failed for %s", self._file_path)

    def append(self, entry: TraceEntry) -> None:
        from aigineering.core.record_conflict import ImmutableRecordConflict

        for existing in self._entries:
            if existing.id != entry.id:
                continue
            if trace_effective_payload(existing) == trace_effective_payload(entry):
                return
            raise ImmutableRecordConflict("trace event", entry.id)
        self._write_line(entry)
        self._entries.append(entry)
        self._seq += 1

    def new_entry(
        self,
        contract_id: str,
        event_type: str,
        **kwargs: object,
    ) -> TraceEntry:
        if kwargs.get("parent_id") is None:
            for e in reversed(self._entries):
                if e.contract_id == contract_id:
                    kwargs["parent_id"] = e.id
                    break
        entry = create_entry(
            contract_id=contract_id,
            event_type=event_type,
            sequence=self._seq,
            **kwargs,  # type: ignore[arg-type]
        )
        self.append(entry)
        return entry

    def get_by_contract(self, contract_id: str) -> list[TraceEntry]:
        return [e for e in self._entries if e.contract_id == contract_id]

    def get_by_event_type(self, event_type: str) -> list[TraceEntry]:
        return [e for e in self._entries if e.event_type == event_type]

    def get_all(self) -> list[TraceEntry]:
        return list(self._entries)

    def get_reverse_lineage(self, asset_id: str) -> list[TraceEntry]:
        return [
            entry for entry in self._entries if _entry_references_asset(entry, asset_id)
        ]


# Backward compatibility: TraceStore alias points to MemoryTraceStore
TraceStore = MemoryTraceStore


def _entry_references_asset(entry: TraceEntry, asset_id: str) -> bool:
    if asset_id in entry.accepted_fragments:
        return True
    if entry.parent_id == asset_id or entry.relation_target == asset_id:
        return True
    for fragment in entry.accepted_fragments:
        if not isinstance(fragment, str) or not fragment.startswith("{"):
            continue
        try:
            payload = json.loads(fragment)
        except json.JSONDecodeError:
            continue
        if payload.get("relation_target") == asset_id:
            return True
    return False


def trace_effective_payload(entry: TraceEntry) -> dict[str, object]:
    """Return the immutable semantic payload of a trace event.

    Timestamp is recording metadata and the ID itself is derived from this
    payload, so neither participates in same-ID replay comparison.
    """

    return {
        "parent_id": entry.parent_id,
        "contract_id": entry.contract_id,
        "event_type": entry.event_type,
        "disclosed_assets": list(entry.disclosed_assets),
        "worker_id": entry.worker_id,
        "candidate_raw": entry.candidate_raw,
        "accepted_fragments": list(entry.accepted_fragments),
        "accepted_asset_names": list(entry.accepted_asset_names),
        "rejected_fragments": list(entry.rejected_fragments),
        "authority_policy": entry.authority_policy,
        "authority_result": entry.authority_result,
        "budget_remaining": entry.budget_remaining,
        "relation_type": entry.relation_type,
        "relation_target": entry.relation_target,
        "usage_metadata": (
            deep_thaw(entry.usage_metadata)
            if entry.usage_metadata is not None
            else None
        ),
    }
