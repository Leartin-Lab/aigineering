"""Application synchronization over committed runtime records."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import collaboration_service

if TYPE_CHECKING:
    from aigineering.core.candidate_decision import CommitmentDecision


class RedisSyncPending(QueryProjectionUnavailable):
    """SQLite committed a decision, but its operational relay is pending."""

    def __init__(
        self,
        *,
        accepted: bool | None = None,
        decision: CommitmentDecision | None = None,
        data: dict | None = None,
        revision: int | None = None,
    ) -> None:
        super().__init__(
            "Redis synchronization pending; committed facts are retained. "
            "Restore Redis and run 'aig redis sync' before continuing."
        )
        self.accepted = accepted
        # These are reporting transports for an already completed SQLite call.
        # They never participate in Candidate admission, replay, or authority.
        self.decision = decision
        self.data = data
        self.revision = revision


def sync_runtime(store, *, accepted: bool | None = None) -> None:
    """Relay committed facts without pretending that Redis shares the transaction."""
    try:
        collaboration_service(store).sync()
    except QueryProjectionUnavailable as exc:
        raise RedisSyncPending(accepted=accepted) from exc


def claim_next_application_package(store, **claim_options):
    """Claim through the shared application ordering and runtime boundary."""
    from aigineering.runtime import claim_next_package

    if claim_options.get("contract_id") is None:
        claim_options["order_contract_ids"] = lambda contract_ids: (
            order_application_contract_ids(store, contract_ids)
        )
    return claim_next_package(store, **claim_options)


def order_application_contract_ids(store, contract_ids):
    """Reorder only the supplied application scope using signed priority facts."""
    from aigineering.task_priority import order_task_contract_ids

    return order_task_contract_ids(store, contract_ids)
