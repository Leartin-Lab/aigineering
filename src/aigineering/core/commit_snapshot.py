"""Compare pure Candidate projections against commit-time Store snapshots."""

from __future__ import annotations

from collections.abc import Callable

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_decision import CommitmentDecision
from aigineering.core.effect_projectors import EffectProjector
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.protocol.candidate import CandidateProposal, GenesisManifest
from aigineering.protocol.candidate import VerifierFactory
from collections.abc import Mapping


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


def record_projection_snapshot_rejection(candidate, decision, conflict, store, trace):
    """Persist a snapshot rejection with only a still-valid receipt."""
    from aigineering.core.candidate_rejection import record_candidate_rejection

    receipt = None
    if not conflict.authentication_rejected:
        receipt = next(
            record
            for record in decision.runtime_records
            if record.record_type == "candidate.received"
        )
    return record_candidate_rejection(
        candidate, str(conflict), store, trace, receipt=receipt
    )


def make_projection_snapshot_validator(
    expected: CommitmentDecision,
    recompute: Callable[[], CommitmentDecision],
) -> Callable[[], None]:
    """Create a Store callback that recomputes the pure decision in its lock."""

    def validate() -> None:
        require_same_projection(expected, recompute())

    return validate


def build_candidate_projection_snapshot_validator(
    candidate: CandidateProposal,
    genesis: GenesisManifest,
    decision: CommitmentDecision,
    store,
    verifier_factory: VerifierFactory,
    additional_effects: Mapping[str, tuple[str, EffectProjector] | EffectHandler]
    | None,
) -> Callable[[], None] | None:
    """Build commit-time validation only when a trusted extension handled input."""
    if (
        not decision.accepted
        or not additional_effects
        or not any(
            effect.effect_type in additional_effects for effect in candidate.effects
        )
    ):
        return None

    def recompute() -> CommitmentDecision:
        from aigineering.core.commitment import reduce_candidate

        actor_keys = load_effective_actor_keys(store, genesis)
        return reduce_candidate(
            candidate,
            genesis,
            verifier_factory=verifier_factory,
            actor_keys=actor_keys,
            projection_context=load_effect_projection_context(
                store, actor_keys=actor_keys
            ),
            additional_effects=additional_effects,
        )

    return make_projection_snapshot_validator(decision, recompute)


def commit_ingress_arguments(decision, candidate, validate_snapshot):
    """Build the stable Store call arguments for an admitted decision."""
    arguments = {
        "accepted_assets": list(decision.assets),
        "trace_entries": list(decision.trace_entries),
        "contracts": decision.contracts,
        "runtime_records": decision.runtime_records,
        "claim_binding": candidate.claim_binding,
        "candidate_actor_id": candidate.actor_id,
        "candidate_key_id": candidate.key_id,
        "candidate_id": candidate.id,
        "candidate_proposal": candidate,
        "effect_fact_owners": decision.effect_fact_owners,
        "assignment_intents": decision.assignment_intents,
        "successor_assignment_witnesses": decision.successor_assignment_witnesses,
    }
    if validate_snapshot is not None:
        arguments["validate_snapshot"] = validate_snapshot
    return arguments
