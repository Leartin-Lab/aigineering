"""Trace event recording and last-entry tracking."""

from __future__ import annotations

from aigineering.core.trace import TraceStoreProtocol, create_entry
from aigineering.protocol.types import TraceEntry


class TraceManager:
    """Record trace events with parent linkage per contract."""

    def __init__(self, trace_store: TraceStoreProtocol) -> None:
        self._store = trace_store
        self._last_entry: dict[str, str] = {}

    @property
    def store(self) -> TraceStoreProtocol:
        """Return the underlying trace store."""
        return self._store

    def record(self, contract_id: str, event_type: str, **kwargs: object) -> TraceEntry:
        """Record a trace event and update last-entry state."""
        parent_id = kwargs.pop("parent_id", None)
        if parent_id is None:
            parent_id = self._last_entry.get(contract_id)
        entry = create_entry(
            contract_id=contract_id,
            event_type=event_type,
            parent_id=parent_id if isinstance(parent_id, str) else None,
            **kwargs,  # type: ignore[arg-type]
        )
        self._store.append(entry)
        self._last_entry[contract_id] = entry.id
        return entry

    def get_last_entry_id(self, contract_id: str) -> str | None:
        """Return the last trace entry id for a contract."""
        return self._last_entry.get(contract_id)

    def get_all_last_entries(self) -> dict[str, str]:
        """Return a copy of last-entry state."""
        return dict(self._last_entry)

    def restore_last_entries(self, entries: dict[str, str]) -> None:
        """Replace last-entry state."""
        self._last_entry = dict(entries)
