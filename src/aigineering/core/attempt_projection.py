"""Pure distinction between a closed claim attempt and task satisfaction."""

from __future__ import annotations

from dataclasses import replace

from aigineering.core.trace import create_entry
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.protocol.candidate import CandidateProposal
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import trace_entry_to_dict


def close_claim_attempt(candidate: CandidateProposal, decision):
    binding = candidate.claim_binding
    if binding is None:
        return decision
    if not decision.accepted:
        outcome = "failed"
    elif decision.contracts:
        outcome = "expanded"
    else:
        outcome = "output_asserted"
    committed = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type in {"candidate.committed", "candidate.rejected"}
        ),
        None,
    )
    parents = (committed.id,) if committed is not None else ()
    records = decision.runtime_records + (
        create_runtime_record(
            "attempt.closed",
            {
                "candidate_id": candidate.id,
                "claim_id": binding.claim_id,
                "contract_id": binding.contract_id,
                "outcome": outcome,
            },
            causal_parents=parents,
        ),
    )
    if outcome == "expanded":
        entry = replace(
            create_entry(
                binding.contract_id,
                "expanded",
                relation_type="candidate",
                relation_target=candidate.id,
                authority_result="accepted",
            ),
            timestamp="",
        )
        records += (
            create_runtime_record(
                "trace.recorded",
                {"trace": trace_entry_to_dict(entry)},
                causal_parents=parents,
            ),
        )
        return replace(
            decision,
            runtime_records=records,
            trace_entries=decision.trace_entries + (entry,),
        )
    if outcome == "failed":
        entry = replace(
            create_entry(
                binding.contract_id,
                "failed",
                relation_type="candidate",
                relation_target=candidate.id,
                authority_result="rejected",
                rejected_fragments=[
                    "[candidate_rejection] claim-bound Candidate was rejected"
                ],
            ),
            timestamp="",
        )
        records += (
            create_terminal_record(
                binding.contract_id,
                "failed",
                reason="claim-bound Candidate was rejected",
                causal_parents=parents,
            ),
            create_runtime_record(
                "trace.recorded",
                {"trace": trace_entry_to_dict(entry)},
                causal_parents=parents,
            ),
        )
        return replace(
            decision,
            runtime_records=records,
            trace_entries=decision.trace_entries + (entry,),
        )
    return replace(decision, runtime_records=records)
