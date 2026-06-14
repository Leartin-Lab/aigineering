"""Append-only TraceStore — the runtime record."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from aigineering.core.ids import now_iso, hash_event
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
) -> TraceEntry:
    entry_id = hash_event(
        contract_id=contract_id,
        event_type=event_type,
        sequence=sequence,
        parent_id=parent_id,
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
        results: list[TraceEntry] = []
        for entry in self.entries:
            if asset_id in entry.accepted_fragments:
                results.append(entry)
        return results


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
        with open(self._file_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    entries.append(trace_entry_from_dict(json.loads(stripped)))
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
        results: list[TraceEntry] = []
        for entry in self._entries:
            if asset_id in entry.accepted_fragments:
                results.append(entry)
        return results


# Backward compatibility: TraceStore alias points to MemoryTraceStore
TraceStore = MemoryTraceStore
