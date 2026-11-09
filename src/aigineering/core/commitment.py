"""Pure Candidate decision and transactional commitment boundary.

Effect projection is delegated; invalid effects have no direct-write fallback.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from aigineering.core.acceptance import (
    OutputQualificationConflict,
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
)
from aigineering.core.candidate_rejection import (
    record_authenticated_candidate_rejection,
    record_candidate_rejection as record_candidate_rejection,
)
from aigineering.core.effect_projection import project_effect_batch
from aigineering.core import projection_context as context_inputs
from aigineering.core.record_conflict import (
    ClaimBindingConflict,
    ImmutableRecordConflict,
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

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import PostCommitTraceExporter, TraceStoreProtocol


def reduce_candidate(
    candidate: CandidateProposal,
    genesis: GenesisManifest,
    *,
    verifier_factory: VerifierFactory,
    actor_keys: tuple[ActorKey, ...] | None = None,
    projection_context: context_inputs.EffectProjectionContext | None = None,
) -> CommitmentDecision:
    """Authenticate and purely decide one Candidate's complete effect batch."""
    validate_genesis_manifest(genesis)
    effective_actor_keys = actor_keys or genesis.root_keys
    effective_context = projection_context or context_inputs.EffectProjectionContext()
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
    return _reduce_received_candidate(
        candidate,
        receipt,
        effective_context,
        effective_actor_keys,
    )


def _reduce_received_candidate(candidate, receipt, context, actor_keys):
    try:
        validate_candidate_usage_metadata(candidate)
    except ValueError as exc:
        return candidate_rejection_decision(candidate, receipt, str(exc))
    try:
        projection = project_effect_batch(
            candidate,
            receipt.id,
            candidate_actor_capabilities(candidate, actor_keys),
            context,
        )
    except (TypeError, ValueError) as exc:
        return candidate_rejection_decision(candidate, receipt, str(exc))
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
        runtime_records=(receipt, *projection.records, trace_record(trace)),
        trace_entries=(trace,),
        contracts=projection.contracts,
        assets=projection.assets,
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
    ) -> None:
        from aigineering.core.store import require_runtime_store
        from aigineering.core.trace import TraceStoreExportAdapter

        self._store = require_runtime_store(store)
        self._trace = store
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
        try:
            with self._store.candidate_transaction():
                decision = self._commit_locked(candidate, genesis, verifier_factory)
        except CandidateCommitRejected as exc:
            rejection = record_authenticated_candidate_rejection(
                candidate, exc.decision, str(exc), self._store, self._trace
            )
            if isinstance(exc.__cause__, ClaimBindingConflict):
                raise CandidateCommitRejected(str(exc), rejection) from exc.__cause__
            decision = rejection
        if self._trace_exporter is not None:
            self._trace_exporter.export(decision.trace_entries)
        return decision

    def _commit_locked(self, candidate, genesis, verifier_factory):
        if genesis is None:
            from aigineering.core.domain import load_genesis

            genesis = load_genesis(self._store)
        actor_keys = load_effective_actor_keys(self._store, genesis)
        try:
            receipt = candidate_received_record(
                candidate,
                genesis,
                verifier_factory=verifier_factory,
                actor_keys=actor_keys,
            )
        except ValueError as exc:
            return record_candidate_rejection(
                candidate, str(exc), self._store, self._trace, receipt=None
            )
        from aigineering.core.publication_history import (
            CandidateHistoryError,
            committed_candidate_decision,
        )

        try:
            historical = self._store.read_candidate_publication(candidate.id)
            if historical:
                return committed_candidate_decision(receipt, historical)
        except CandidateHistoryError:
            raise
        except (TypeError, ValueError) as exc:
            raise CandidateHistoryError(
                "Candidate committed history is incomplete or invalid"
            ) from exc
        projection_context = context_inputs.load_candidate_projection_context(
            self._store, candidate, actor_keys=actor_keys
        )
        decision = _reduce_received_candidate(
            candidate,
            receipt,
            projection_context,
            actor_keys,
        )
        decision = close_claim_attempt(candidate, decision)
        try:
            derived_traces, derived_records = self._store.commit_ingress_batch(
                accepted_assets=list(decision.assets),
                trace_entries=list(decision.trace_entries),
                contracts=decision.contracts,
                runtime_records=decision.runtime_records,
                claim_binding=candidate.claim_binding,
                candidate_actor_id=candidate.actor_id,
                candidate_key_id=candidate.key_id,
                candidate_id=candidate.id,
                candidate_proposal=candidate,
            )
            if derived_traces or derived_records:
                decision = replace(
                    decision,
                    trace_entries=decision.trace_entries + tuple(derived_traces),
                    runtime_records=decision.runtime_records + tuple(derived_records),
                )
        except (
            ClaimBindingConflict,
            CausalAllowanceConflict,
            ImmutableRecordConflict,
            OutputQualificationConflict,
        ) as exc:
            raise CandidateCommitRejected(str(exc), decision) from exc
        return decision
