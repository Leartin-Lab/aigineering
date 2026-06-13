"""RuntimeTransaction — atomic multi-store transaction for candidate submission (G3).

One submit transaction must atomically cover: accepted assets, trace events,
idempotency key, claim transition, and completion state. This replaces the
current non-atomic multi-store writes in ``submit.py``.

Gate: G3 (Transactional Runtime Substrate)
"""

from __future__ import annotations

from typing import Any


class RuntimeTransactionError(Exception):
    """Raised when a runtime transaction fails or is used after commit/rollback."""


class RuntimeTransaction:
    """In-memory transaction buffer for atomic candidate submission.

    Operations are queued in memory and committed atomically to the backing
    stores. If any operation fails, the entire transaction rolls back.

    This is the 040 prototype — full SQLite-backed transactions arrive in 050+.
    """

    def __init__(self) -> None:
        self._committed = False
        self._rolled_back = False
        self._assets: list[Any] = []
        self._contracts: list[Any] = []
        self._trace_entries: list[dict[str, Any]] = []
        self._idempotency: dict[str, str] = {}  # contract_id → result_key
        self._claims: list[dict[str, Any]] = []
        self._completions: set[str] = set()

    def add_asset(self, asset: Any) -> None:
        """Queue an asset for commit."""
        self._check_active()
        self._assets.append(asset)

    def add_contract(self, contract: Any) -> None:
        """Queue a contract for commit."""
        self._check_active()
        self._contracts.append(contract)

    def append_trace(self, contract_id: str, event_type: str, **kwargs: Any) -> None:
        """Queue a trace entry for commit."""
        self._check_active()
        self._trace_entries.append({
            "contract_id": contract_id,
            "event_type": event_type,
            **kwargs,
        })

    def set_idempotency(self, contract_id: str, key: str) -> None:
        """Record an idempotency key."""
        self._check_active()
        self._idempotency[contract_id] = key

    def mark_complete(self, contract_id: str) -> None:
        """Mark a contract as completed."""
        self._check_active()
        self._completions.add(contract_id)

    def commit(
        self,
        store: Any,
        trace_store: Any,
        idempotency_store: Any | None = None,
        claim_store: Any | None = None,
    ) -> None:
        """Atomically commit all queued operations to the backing stores.

        If any single operation fails, the implementation should roll back
        previously committed operations.  The current in-memory version
        relies on the caller's exception handling for rollback.
        """
        self._check_active()

        try:
            for asset in self._assets:
                store.add_asset(asset)

            for contract in self._contracts:
                store.add_contract(contract)

            for entry in self._trace_entries:
                from aigineering.core.trace import create_entry
                trace_entry = create_entry(**entry)
                trace_store.append(trace_entry)

            for contract_id, key in self._idempotency.items():
                if idempotency_store is not None:
                    idempotency_store.set(contract_id, key)

            for contract_id in self._completions:
                trace_entry = create_entry(
                    contract_id=contract_id,
                    event_type="complete",
                )
                trace_store.append(trace_entry)

        except Exception:
            self._rolled_back = True
            raise

        self._committed = True

    def rollback(self) -> None:
        """Discard all queued operations."""
        self._assets.clear()
        self._contracts.clear()
        self._trace_entries.clear()
        self._idempotency.clear()
        self._claims.clear()
        self._completions.clear()
        self._rolled_back = True

    def _check_active(self) -> None:
        if self._committed:
            raise RuntimeTransactionError("Transaction already committed")
        if self._rolled_back:
            raise RuntimeTransactionError("Transaction already rolled back")
