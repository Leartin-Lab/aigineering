"""Pure Candidate decision and transactional commitment boundary.

Effect projection is delegated; invalid effects have no direct-write fallback.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from aigineering.core.acceptance import materialize_qualification_facts
from aigineering.core.actor_facts import load_effective_actor_keys
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
from aigineering.core.effect_projection import project_effect_batch
from aigineering.core.projection_context import (
    EffectProjectionContext,
    load_effect_projection_context,
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
    validate_genesis_manifest,
)
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol


def _actor_capabilities(
    candidate: CandidateProposal, actor_keys: tuple[ActorKey, ...]
) -> tuple[str, ...]:
    key = next(
        item
        for item in actor_keys
        if item.actor_id == candidate.actor_id and item.key_id == candidate.key_id
    )
    return key.capabilities


def reduce_candidate(
    candidate: CandidateProposal,
    genesis: GenesisManifest,
    *,
    verifier_factory: VerifierFactory,
    actor_keys: tuple[ActorKey, ...] | None = None,
    projection_context: EffectProjectionContext | None = None,
) -> CommitmentDecision:
    """Authenticate and purely decide one Candidate's complete effect batch."""
    validate_genesis_manifest(genesis)
    effective_actor_keys = actor_keys or genesis.root_keys
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
        projection = project_effect_batch(
            candidate,
            receipt.id,
            _actor_capabilities(candidate, effective_actor_keys),
            projection_context,
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
        contract_id="commitment",
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
    )
    return CommitmentDecision(
        candidate_id=candidate.id,
        accepted=True,
        runtime_records=(receipt, *projection.records, committed, trace_record(trace)),
        trace_entries=(trace,),
        contracts=projection.contracts,
        assets=projection.assets,
    )


class CandidateCommitter:
    """Store-independent transactional adapter for the pure reducer."""

    def __init__(self, store: StoreProtocol, trace: TraceStoreProtocol) -> None:
        from aigineering.core.store import require_runtime_store

        self._store = require_runtime_store(store)
        self._trace = trace

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
        decision = reduce_candidate(
            candidate,
            genesis,
            verifier_factory=verifier_factory,
            actor_keys=load_effective_actor_keys(self._store, genesis),
            projection_context=load_effect_projection_context(self._store),
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
            self._store.commit_ingress_batch(
                accepted_assets=list(decision.assets),
                trace_entries=list(decision.trace_entries),
                contracts=decision.contracts,
                runtime_records=decision.runtime_records,
                claim_binding=candidate.claim_binding,
                candidate_actor_id=candidate.actor_id,
                candidate_key_id=candidate.key_id,
                candidate_id=candidate.id,
            )
        except CausalAllowanceConflict as exc:
            receipt = next(
                record
                for record in decision.runtime_records
                if record.record_type == "candidate.received"
            )
            return record_candidate_rejection(
                candidate, str(exc), self._store, self._trace, receipt=receipt
            )
        if self._trace is not self._store:
            for entry in decision.trace_entries:
                self._trace.append(entry)
        return decision


def record_candidate_rejection(
    candidate: CandidateProposal,
    reason: str,
    store: StoreProtocol,
    trace: TraceStoreProtocol,
    *,
    receipt: RuntimeRecord | None = None,
) -> CommitmentDecision:
    """Durably record a failed Candidate without interpreting its effects."""
    from aigineering.core.store import require_runtime_store

    decision = (
        candidate_rejection_decision(candidate, receipt, reason)
        if receipt is not None
        else authentication_rejection_decision(candidate, reason)
    )
    runtime_store = require_runtime_store(store)
    runtime_store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=list(decision.trace_entries),
        runtime_records=decision.runtime_records,
    )
    if trace is not store:
        for entry in decision.trace_entries:
            trace.append(entry)
    return decision
