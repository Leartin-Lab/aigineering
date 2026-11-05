"""Immutable Candidate decision values and rejection projections."""

from __future__ import annotations

from dataclasses import dataclass, replace

from aigineering.core.trace import create_entry, trace_record_from_entry
from aigineering.protocol.candidate import CandidateProposal, candidate_usage_metadata
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Asset, Contract, TraceEntry
from aigineering.core.claim_assignments import (
    AssignmentIntent,
    EffectFactOwner,
    SuccessorAssignmentWitness,
)


@dataclass(frozen=True)
class CommitmentDecision:
    """Pure result of reducing one authenticated Candidate."""

    candidate_id: str
    accepted: bool
    runtime_records: tuple[RuntimeRecord, ...]
    trace_entries: tuple[TraceEntry, ...]
    contracts: tuple[Contract, ...] = ()
    assets: tuple[Asset, ...] = ()
    effect_fact_owners: tuple[EffectFactOwner, ...] = ()
    assignment_intents: tuple[AssignmentIntent, ...] = ()
    successor_assignment_witnesses: tuple[SuccessorAssignmentWitness, ...] = ()

    @property
    def contract(self) -> Contract | None:
        """Compatibility view for a Candidate declaring exactly one Contract."""
        return self.contracts[0] if len(self.contracts) == 1 else None


class ProjectionSnapshotConflict(ValueError):
    """An injected effect no longer projects to the facts admitted earlier."""

    def __init__(self, reason: str, *, authentication_rejected: bool = False) -> None:
        self.authentication_rejected = authentication_rejected
        super().__init__(reason)


def decision_projection_fingerprint(decision: CommitmentDecision) -> tuple:
    """Return stable identities for the pure portion of a Candidate decision."""
    return (
        tuple(contract.id for contract in decision.contracts),
        tuple(asset.id for asset in decision.assets),
        tuple(record.id for record in decision.runtime_records),
        tuple(entry.id for entry in decision.trace_entries),
        tuple(
            (owner.record_id, owner.effect_type, owner.assignment_admission.value)
            for owner in decision.effect_fact_owners
        ),
        tuple(
            (
                item.assignment_record_id,
                item.target_contract_id,
                item.target_declaration_record_id,
                item.mode.value,
            )
            for item in decision.assignment_intents
        ),
        tuple(
            (
                item.source_contract_id,
                item.source_assignment_head_id,
                item.source_terminal_record_id,
                item.successor_contract_id,
                item.successor_declaration_record_id,
                item.successor_assignment_record_id,
                item.successor_link_record_id,
                item.assignment_mode.value,
            )
            for item in decision.successor_assignment_witnesses
        ),
    )


def require_same_projection(
    expected: CommitmentDecision, actual: CommitmentDecision
) -> None:
    """Reject when current facts change a previously accepted pure projection."""
    if not actual.accepted:
        authentication_rejection = next(
            (
                record
                for record in actual.runtime_records
                if record.record_type == "candidate.authentication_rejected"
            ),
            None,
        )
        rejection = next(
            (
                record
                for record in actual.runtime_records
                if record.record_type == "candidate.rejected"
            ),
            None,
        )
        reason = (
            str(authentication_rejection.payload.get("reason", ""))[:256]
            if authentication_rejection is not None
            else str(rejection.payload.get("reason", ""))[:256]
            if rejection is not None
            else "current projection is no longer admissible"
        )
        raise ProjectionSnapshotConflict(
            f"commit-time projection rejected: {reason}",
            authentication_rejected=authentication_rejection is not None,
        )
    if decision_projection_fingerprint(expected) != decision_projection_fingerprint(
        actual
    ):
        raise ProjectionSnapshotConflict(
            "commit-time projection changed before transactional commitment"
        )


def trace_record(
    entry: TraceEntry,
    *,
    causal_parents: tuple[str, ...] | list[str] = (),
    recorded_at: str | None = None,
) -> RuntimeRecord:
    """Encode one TraceEntry as its canonical durable RuntimeRecord."""
    return trace_record_from_entry(
        entry,
        causal_parents=causal_parents,
        recorded_at=recorded_at,
    )


def candidate_trace(**kwargs) -> TraceEntry:
    """Candidate decisions are pure; commit time lives on RuntimeRecord."""
    return replace(create_entry(**kwargs), timestamp="")


def candidate_rejection_decision(
    candidate: CandidateProposal,
    receipt: RuntimeRecord,
    reason: str,
) -> CommitmentDecision:
    payload = {
        "candidate_id": candidate.id,
        "reason": reason,
        "effect_types": [effect.effect_type for effect in candidate.effects],
    }
    if candidate.claim_binding is not None:
        payload["contract_id"] = candidate.claim_binding.contract_id
        payload["claim_id"] = candidate.claim_binding.claim_id
    rejection = create_runtime_record(
        "candidate.rejected",
        payload,
        causal_parents=(receipt.id,),
    )
    trace = candidate_trace(
        contract_id=(
            candidate.claim_binding.contract_id
            if candidate.claim_binding is not None
            else "commitment"
        ),
        event_type="candidate_rejected",
        parent_id=candidate.id,
        worker_id=candidate.actor_id,
        authority_result="rejected",
        rejected_fragments=[f"[candidate_rejection] {reason}"],
        usage_metadata=candidate_usage_metadata(candidate) or None,
    )
    return CommitmentDecision(
        candidate_id=candidate.id,
        accepted=False,
        runtime_records=(receipt, rejection, trace_record(trace)),
        trace_entries=(trace,),
    )


def authentication_rejection_decision(
    candidate: CandidateProposal, reason: str
) -> CommitmentDecision:
    rejection = create_runtime_record(
        "candidate.authentication_rejected",
        {
            "claimed_actor_id": candidate.actor_id,
            "claimed_candidate_id": candidate.id,
            "reason": reason,
            "signature_kind": candidate.signature_kind,
        },
    )
    trace = candidate_trace(
        contract_id="commitment",
        event_type="candidate_authentication_rejected",
        parent_id=candidate.id,
        worker_id=candidate.actor_id,
        authority_result="rejected",
        rejected_fragments=[f"[authentication_rejection] {reason}"],
    )
    return CommitmentDecision(
        candidate_id=candidate.id,
        accepted=False,
        runtime_records=(rejection, trace_record(trace)),
        trace_entries=(trace,),
    )
