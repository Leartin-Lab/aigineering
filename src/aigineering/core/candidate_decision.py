"""Immutable Candidate decision values and rejection projections."""

from __future__ import annotations

from dataclasses import dataclass, replace

from aigineering.core.trace import create_entry
from aigineering.protocol.candidate import CandidateProposal
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Asset, Contract, TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict


@dataclass(frozen=True)
class CommitmentDecision:
    """Pure result of reducing one authenticated Candidate."""

    candidate_id: str
    accepted: bool
    runtime_records: tuple[RuntimeRecord, ...]
    trace_entries: tuple[TraceEntry, ...]
    contracts: tuple[Contract, ...] = ()
    assets: tuple[Asset, ...] = ()

    @property
    def contract(self) -> Contract | None:
        """Compatibility view for a Candidate declaring exactly one Contract."""
        return self.contracts[0] if len(self.contracts) == 1 else None


def trace_record(entry: TraceEntry) -> RuntimeRecord:
    return create_runtime_record(
        "trace.recorded", {"trace": trace_entry_to_dict(entry)}
    )


def candidate_trace(**kwargs) -> TraceEntry:
    """Candidate decisions are pure; commit time lives on RuntimeRecord."""
    return replace(create_entry(**kwargs), timestamp="")


def candidate_rejection_decision(
    candidate: CandidateProposal,
    receipt: RuntimeRecord,
    reason: str,
) -> CommitmentDecision:
    rejection = create_runtime_record(
        "candidate.rejected",
        {
            "candidate_id": candidate.id,
            "reason": reason,
            "effect_types": [effect.effect_type for effect in candidate.effects],
        },
        causal_parents=(receipt.id,),
    )
    trace = candidate_trace(
        contract_id="commitment",
        event_type="candidate_rejected",
        parent_id=candidate.id,
        worker_id=candidate.actor_id,
        authority_result="rejected",
        rejected_fragments=[f"[candidate_rejection] {reason}"],
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
