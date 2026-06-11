"""Worker heartbeat for presence renewal and lease extension (v0.3.16).

Heartbeat is a presence signal ONLY. It extends leases on active claims but
**never** dispatches work assignments. Work dispatch is the responsibility
of the engine/planner layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aigineering.core.claims import ClaimStore
from aigineering.core.ids import now_iso


@dataclass
class Heartbeat:
    """Live presence record for a worker.

    .. note::

       Heartbeat is purely observational — it records presence and extends
       leases. It does **not** assign new contracts or dispatch work.
    """

    worker_id: str
    capabilities: tuple[str, ...]  # capability summary
    last_seen: str  # ISO 8601 timestamp
    active_claim_ids: tuple[str, ...]
    renewal_deadline: str  # ISO 8601 — when the next heartbeat is expected

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "active_claim_ids", tuple(self.active_claim_ids))


class HeartbeatStore:
    """Manages worker heartbeats and extends claim leases.

    HeartbeatStore does **not** dispatch work. Its sole purpose is worker
    presence tracking and lease renewal.
    """

    def __init__(self, claim_store: ClaimStore) -> None:
        self._claim_store = claim_store
        self._heartbeats: dict[str, Heartbeat] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def heartbeat(
        self,
        worker_id: str,
        capabilities: tuple[str, ...] | None = None,
        claim_ids: tuple[str, ...] | None = None,
    ) -> Heartbeat:
        """Record a heartbeat and extend leases on active claims.

        This is a **presence signal only**. No work is dispatched.

        Returns the updated ``Heartbeat`` record.
        """
        caps = tuple(capabilities) if capabilities is not None else ()
        cids = tuple(claim_ids) if claim_ids is not None else ()
        now = now_iso()

        # Extend leases for all active claims
        for cid in cids:
            self._claim_store.extend_lease(cid, extend_seconds=30)

        # Renewal deadline: 30 s from now (next expected heartbeat)
        from datetime import datetime, timezone

        deadline_dt = datetime.now(timezone.utc).timestamp() + 30
        deadline = datetime.fromtimestamp(deadline_dt, tz=timezone.utc).isoformat()

        hb = Heartbeat(
            worker_id=worker_id,
            capabilities=caps,
            last_seen=now,
            active_claim_ids=cids,
            renewal_deadline=deadline,
        )
        self._heartbeats[worker_id] = hb
        return hb

    def renew_lease(self, claim_id: str, extend_seconds: int = 30) -> bool:
        """Extend the lease on *claim_id* by *extend_seconds*.

        Delegates to the underlying ``ClaimStore``. Only active claims
        can have their leases extended.

        Returns ``True`` if the lease was extended.
        """
        return self._claim_store.extend_lease(claim_id, extend_seconds)

    def get_heartbeat(self, worker_id: str) -> Heartbeat | None:
        """Return the last heartbeat for *worker_id*, or *None*."""
        return self._heartbeats.get(worker_id)
