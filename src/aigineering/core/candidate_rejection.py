"""Durable audit recording for rejected Candidate proposals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.candidate_decision import (
    CommitmentDecision,
    authentication_rejection_decision,
    candidate_rejection_decision,
)
from aigineering.protocol.candidate import CandidateProposal
from aigineering.protocol.runtime_record import RuntimeRecord

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol


def record_candidate_rejection(
    candidate: CandidateProposal,
    reason: str,
    store: StoreProtocol,
    trace: TraceStoreProtocol,
    *,
    receipt: RuntimeRecord | None = None,
) -> CommitmentDecision:
    """Persist the rejection and its trace without interpreting effects."""
    decision = (
        candidate_rejection_decision(candidate, receipt, reason)
        if receipt is not None
        else authentication_rejection_decision(candidate, reason)
    )
    from aigineering.core.store import require_runtime_store

    runtime_store = require_runtime_store(store)
    runtime_store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=list(decision.trace_entries),
        runtime_records=decision.runtime_records,
    )
    if trace is not store:
        from aigineering.core.trace import TraceStoreExportAdapter

        TraceStoreExportAdapter(trace).export(decision.trace_entries)
    return decision


def record_unreceipted_authentication_rejection(
    candidate: CandidateProposal,
    decision: CommitmentDecision,
    store: StoreProtocol,
) -> CommitmentDecision | None:
    """Audit an unauthenticated decision without admitting any Candidate headers."""
    if decision.accepted:
        return None
    authentication_rejection = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type == "candidate.authentication_rejected"
        ),
        None,
    )
    has_authenticated_receipt = any(
        record.record_type == "candidate.received"
        for record in decision.runtime_records
    )
    if authentication_rejection is None or has_authenticated_receipt:
        return None
    return record_candidate_rejection(
        candidate,
        str(authentication_rejection.payload.get("reason", "authentication failed")),
        store,
        store,
        receipt=None,
    )


def record_authenticated_candidate_rejection(
    candidate: CandidateProposal,
    decision: CommitmentDecision,
    reason: str,
    store: StoreProtocol,
    trace: TraceStoreProtocol,
) -> CommitmentDecision:
    """Record a post-authentication rejection against its exact receipt."""
    receipts = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.received"
    )
    if len(receipts) != 1 or receipts[0].payload.get("candidate_id") != candidate.id:
        raise ValueError(
            "authenticated rejection requires its unique Candidate receipt"
        )
    return record_candidate_rejection(
        candidate, reason, store, trace, receipt=receipts[0]
    )
