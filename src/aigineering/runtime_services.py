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
        identifiers: dict | None = None,
        revision: int | None = None,
        reason: str = "redis_unavailable",
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
        self.identifiers = identifiers
        self.revision = revision
        self.reason = reason

    def committed_identifiers(self) -> dict:
        """Render only exact SQLite identifiers, without signed material."""
        identifiers = dict(self.identifiers or {})
        if self.revision is not None:
            identifiers["revision"] = self.revision
        if self.decision is None:
            return identifiers
        decision = self.decision
        asset_ids = dict.fromkeys(asset.id for asset in decision.assets)
        contract_ids = dict.fromkeys(contract.id for contract in decision.contracts)
        for record in decision.runtime_records:
            if (
                record.record_type == "candidate.received"
                and record.payload.get("candidate_id") == decision.candidate_id
            ):
                identifiers["candidate_id"] = decision.candidate_id
                identifiers["receipt_id"] = record.id
            elif (
                record.record_type == "candidate.committed"
                and record.payload.get("candidate_id") == decision.candidate_id
            ):
                identifiers["head_id"] = record.id
            elif record.record_type in {
                "candidate.rejected",
                "candidate.authentication_rejected",
            }:
                identifiers["rejection_id"] = record.id
            elif record.record_type == "asset.attested":
                asset_ids[record.payload["asset_id"]] = None
                contract_ids[record.payload["contract_id"]] = None
            elif record.record_type == "lifecycle.terminal":
                contract_ids[record.payload["contract_id"]] = None
            elif record.record_type == "claim.submitted":
                identifiers["claim_id"] = record.payload["claim_id"]
                contract_ids[record.payload["contract_id"]] = None
            elif record.record_type == "replacement.claimed":
                identifiers.setdefault("replacement_claim_ids", []).append(
                    record.payload["claim"]["id"]
                )
        if asset_ids:
            identifiers["asset_ids"] = list(asset_ids)
        if contract_ids:
            identifiers["contract_ids"] = list(contract_ids)
        return identifiers


def sync_runtime(
    store,
    *,
    accepted: bool | None = None,
    decision: CommitmentDecision | None = None,
    identifiers: dict | None = None,
) -> None:
    """Relay committed facts without pretending that Redis shares the transaction."""
    try:
        collaboration_service(store).sync()
    except QueryProjectionUnavailable as exc:
        raise RedisSyncPending(
            accepted=decision.accepted if decision is not None else accepted,
            decision=decision,
            identifiers=identifiers,
            revision=(
                store.get_runtime_revision()
                if decision is not None or identifiers
                else None
            ),
            reason=getattr(exc, "reason", "redis_unavailable"),
        ) from exc


def claim_next_application_package(store, **claim_options):
    """Claim through the shared application ordering and runtime boundary."""
    from aigineering.runtime import claim_next_package

    if claim_options.get("contract_id") is None:
        claim_options["order_contract_ids"] = lambda contract_ids: (
            order_application_contract_ids(store, contract_ids)
        )
    return claim_next_package(store, **claim_options)


def order_application_contract_ids(store, contract_ids):
    """Keep the declared application scope in its existing stable order."""
    return tuple(contract_ids)
