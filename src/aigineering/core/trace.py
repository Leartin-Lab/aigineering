"""Append-only TraceStore — the runtime record."""

from __future__ import annotations

from typing import Optional

from aigineering.core.ids import now_iso, trace_entry_id
from aigineering.protocol.types import TraceEntry


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
    rejected_fragments: Optional[list[str]] = None,
    authority_policy: Optional[str] = None,
    authority_result: Optional[bool] = None,
    budget_remaining: int = 0,
) -> TraceEntry:
    entry_id = trace_entry_id(
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
        rejected_fragments=rejected_fragments if rejected_fragments is not None else [],
        authority_policy=authority_policy,
        authority_result=authority_result,
        budget_remaining=budget_remaining,
        timestamp=now_iso(),
    )


class TraceStore:
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
