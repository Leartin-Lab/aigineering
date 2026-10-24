"""Pure Candidate decision and transactional commitment boundary.

Effect projection is delegated; invalid effects have no direct-write fallback.
"""

from __future__ import annotations

import json
from dataclasses import replace
from collections.abc import Mapping
from typing import TYPE_CHECKING

from aigineering.core.acceptance import (
    OutputQualificationConflict,
    materialize_qualification_facts,
)
from aigineering.core.actor_facts import (
    candidate_actor_capabilities,
    load_effective_actor_keys,
)
from aigineering.core.attempt_projection import close_claim_attempt
from aigineering.core.candidate_decision import (
    CommitmentDecision,
    authentication_rejection_decision,
    candidate_rejection_decision,
    candidate_trace,
    trace_record,
)
from aigineering.core.causal_allowance import (
    CausalAllowanceConflict,
    materialize_terminal_allowance,
)
from aigineering.core.candidate_rejection import (
    record_authenticated_candidate_rejection,
    record_candidate_rejection as record_candidate_rejection,
    record_unreceipted_authentication_rejection,
)
from aigineering.core.claim_holds import ClaimHoldConflict
from aigineering.core.claim_assignments import AssignmentConflict
from aigineering.core.claim_constraints import ClaimConstraintConflict
from aigineering.core.commit_snapshot import (
    ProjectionSnapshotConflict,
    build_candidate_projection_snapshot_validator,
    commit_ingress_arguments,
    record_projection_snapshot_rejection,
)
from aigineering.core.effect_projection import (
    freeze_additional_effects,
    project_effect_batch,
)
from aigineering.core.effect_projectors import EffectProjector
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.projection_context import (
    EffectProjectionContext,
    load_contract_declaration_projection_context,
)
from aigineering.core.record_conflict import (
    ClaimBindingConflict,
    ImmutableRecordConflict,
)
from aigineering.core.fact_materialization import (
    reduce_asset_facts,
    trace_records,
)
from aigineering.core.signing import create_verifier
from aigineering.protocol.candidate import (
    CandidateProposal,
    ActorKey,
    GenesisManifest,
    VerifierFactory,
    candidate_received_record,
    candidate_usage_metadata,
    validate_genesis_manifest,
    validate_candidate_usage_metadata,
)
from aigineering.protocol.runtime_record import create_runtime_record

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import PostCommitTraceExporter, TraceStoreProtocol


def reduce_candidate(
    candidate: CandidateProposal,
    genesis: GenesisManifest,
    *,
    verifier_factory: VerifierFactory,
    actor_keys: tuple[ActorKey, ...] | None = None,
    projection_context: EffectProjectionContext | None = None,
    additional_effects: Mapping[str, tuple[str, EffectProjector] | EffectHandler]
    | None = None,
) -> CommitmentDecision:
    """Authenticate and purely decide one Candidate's complete effect batch."""
    validate_genesis_manifest(genesis)
    effective_actor_keys = actor_keys or genesis.root_keys
    effective_context = projection_context or EffectProjectionContext()
    if not effective_context.actor_keys:
        effective_context = replace(effective_context, actor_keys=effective_actor_keys)
    try:
        receipt = candidate_received_record(
            candidate,
            genesis,
            verifier_factory=verifier_factory,
            actor_keys=effective_actor_keys,
        )
    except ValueError as exc:
        return authentication_rejection_decision(candidate, str(exc))
    try:
        validate_candidate_usage_metadata(candidate)
    except ValueError as exc:
        return candidate_rejection_decision(candidate, receipt, str(exc))
    try:
        projection = project_effect_batch(
            candidate,
            receipt.id,
            candidate_actor_capabilities(candidate, effective_actor_keys),
            effective_context,
            additional_effects=additional_effects,
        )
    except (TypeError, ValueError) as exc:
        return candidate_rejection_decision(candidate, receipt, str(exc))
    committed = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [record.id for record in projection.records],
        },
        causal_parents=(receipt.id, *(record.id for record in projection.records)),
    )
    trace = candidate_trace(
        contract_id=(
            candidate.claim_binding.contract_id
            if candidate.claim_binding is not None
            else "commitment"
        ),
        event_type="candidate_committed",
        parent_id=candidate.id,
        worker_id=candidate.actor_id,
        relation_target=projection.relation_target,
        authority_result="accepted",
        accepted_asset_names=list(projection.accepted_asset_names),
        accepted_fragments=[
            json.dumps(
                {
                    "candidate_id": candidate.id,
                    "effect_type": effect_type,
                    "relation_target": relation_target,
                },
                sort_keys=True,
            )
            for effect_type, relation_target in projection.projected_effects
        ],
        usage_metadata=candidate_usage_metadata(candidate) or None,
    )
    return CommitmentDecision(
        candidate_id=candidate.id,
        accepted=True,
        runtime_records=(receipt, *projection.records, committed, trace_record(trace)),
        trace_entries=(trace,),
        contracts=projection.contracts,
        assets=projection.assets,
        effect_fact_owners=projection.effect_fact_owners,
        assignment_intents=projection.assignment_intents,
        successor_assignment_witnesses=projection.successor_assignment_witnesses,
    )


class CandidateCommitRejected(ValueError):
    """A commit predicate rejected the Candidate after authentication."""

    def __init__(self, reason: str, decision: CommitmentDecision) -> None:
        self.decision = decision
        super().__init__(reason)


class CandidateCommitter:
    """Store-independent transactional adapter for the pure reducer."""

    def __init__(
        self,
        store: StoreProtocol,
        trace: TraceStoreProtocol,
        *,
        additional_effects: Mapping[str, tuple[str, EffectProjector] | EffectHandler]
        | None = None,
    ) -> None:
        from aigineering.core.store import require_runtime_store
        from aigineering.core.trace import TraceStoreExportAdapter

        self._store = require_runtime_store(store)
        self._trace = store
        self._additional_effects = freeze_additional_effects(additional_effects)
        self._trace_exporter: PostCommitTraceExporter | None = (
            None if trace is store else TraceStoreExportAdapter(trace)
        )

    def commit(
        self,
        candidate: CandidateProposal,
        genesis: GenesisManifest | None = None,
        *,
        verifier_factory: VerifierFactory = create_verifier,
    ) -> CommitmentDecision:
        if genesis is None:
            from aigineering.core.domain import load_genesis

            genesis = load_genesis(self._store)
        actor_keys = load_effective_actor_keys(self._store, genesis)
        decision = reduce_candidate(
            candidate,
            genesis,
            verifier_factory=verifier_factory,
            actor_keys=actor_keys,
            projection_context=load_contract_declaration_projection_context(
                self._store, candidate, actor_keys=actor_keys
            ),
            additional_effects=self._additional_effects,
        )
        rejected = record_unreceipted_authentication_rejection(
            candidate, decision, self._store
        )
        if rejected is not None:
            if self._trace_exporter is not None:
                self._trace_exporter.export(rejected.trace_entries)
            return rejected
        validate_snapshot = build_candidate_projection_snapshot_validator(
            candidate,
            genesis,
            decision,
            self._store,
            verifier_factory,
            self._additional_effects,
        )
        decision = close_claim_attempt(candidate, decision)
        if decision.assets:
            reducer_traces, reducer_records = reduce_asset_facts(
                self._store,
                self._trace,
                decision.assets,
                pending_contracts=decision.contracts,
            )
            decision = replace(
                decision,
                trace_entries=decision.trace_entries + tuple(reducer_traces),
                runtime_records=decision.runtime_records
                + reducer_records
                + trace_records(reducer_traces),
            )
        qualification_traces, qualification_records = materialize_qualification_facts(
            self._store, decision.runtime_records
        )
        if qualification_traces or qualification_records:
            decision = replace(
                decision,
                trace_entries=decision.trace_entries + qualification_traces,
                runtime_records=decision.runtime_records + qualification_records,
            )
        decision = replace(
            decision,
            runtime_records=decision.runtime_records
            + materialize_terminal_allowance(
                self._store, decision.contracts, decision.runtime_records
            ),
        )
        try:
            derived_traces, derived_records = self._store.commit_ingress_batch(
                **commit_ingress_arguments(decision, candidate, validate_snapshot),
            )
            if derived_traces or derived_records:
                decision = replace(
                    decision,
                    trace_entries=decision.trace_entries + tuple(derived_traces),
                    runtime_records=decision.runtime_records + tuple(derived_records),
                )
        except (
            ClaimBindingConflict,
            ClaimHoldConflict,
            ClaimConstraintConflict,
            AssignmentConflict,
        ) as exc:
            rejection = record_authenticated_candidate_rejection(
                candidate, decision, str(exc), self._store, self._trace
            )
            raise CandidateCommitRejected(str(exc), rejection) from exc
        except ProjectionSnapshotConflict as exc:
            return record_projection_snapshot_rejection(
                candidate, decision, exc, self._store, self._trace
            )
        except (
            CausalAllowanceConflict,
            ImmutableRecordConflict,
            OutputQualificationConflict,
        ) as exc:
            return record_authenticated_candidate_rejection(
                candidate, decision, str(exc), self._store, self._trace
            )
        if self._trace_exporter is not None:
            self._trace_exporter.export(decision.trace_entries)
        return decision
