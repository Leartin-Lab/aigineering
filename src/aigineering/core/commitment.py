"""Pure Candidate decision and transactional commitment boundary.

Built-in effect projection is delegated to a closed registry. Unsupported or
invalid effects are rejected as a whole and recorded visibly; there is no
fallback to direct RuntimeIngress writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aigineering.core.effect_projection import BUILTIN_EFFECTS
from aigineering.core.fact_materialization import (
    materialize_fact_reduction,
    trace_records,
)
from aigineering.core.trace import create_entry
from aigineering.core.signing import create_verifier
from aigineering.protocol.candidate import (
    CandidateProposal,
    GenesisManifest,
    VerifierFactory,
    candidate_received_record,
    validate_genesis_manifest,
)
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Asset, Contract, TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol


@dataclass(frozen=True)
class CommitmentDecision:
    """Pure result of reducing one authenticated Candidate."""

    candidate_id: str
    accepted: bool
    runtime_records: tuple[RuntimeRecord, ...]
    trace_entries: tuple[TraceEntry, ...]
    contract: Contract | None = None
    assets: tuple[Asset, ...] = ()


def _actor_capabilities(
    candidate: CandidateProposal, genesis: GenesisManifest
) -> tuple[str, ...]:
    key = next(
        item
        for item in genesis.root_keys
        if item.actor_id == candidate.actor_id and item.key_id == candidate.key_id
    )
    return key.capabilities


def _trace_record(entry: TraceEntry) -> RuntimeRecord:
    return create_runtime_record(
        "trace.recorded", {"trace": trace_entry_to_dict(entry)}
    )


def _candidate_trace(**kwargs) -> TraceEntry:
    """Candidate decisions are pure; commit time lives on RuntimeRecord."""
    return replace(create_entry(**kwargs), timestamp="")


def _rejection_decision(
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
    trace = _candidate_trace(
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
        runtime_records=(receipt, rejection, _trace_record(trace)),
        trace_entries=(trace,),
    )


def _authentication_rejection_decision(
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
    trace = _candidate_trace(
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
        runtime_records=(rejection, _trace_record(trace)),
        trace_entries=(trace,),
    )


def reduce_candidate(
    candidate: CandidateProposal,
    genesis: GenesisManifest,
    *,
    verifier_factory: VerifierFactory,
) -> CommitmentDecision:
    """Authenticate and purely decide one Candidate's complete effect batch."""
    validate_genesis_manifest(genesis)
    try:
        receipt = candidate_received_record(
            candidate, genesis, verifier_factory=verifier_factory
        )
    except ValueError as exc:
        return _authentication_rejection_decision(candidate, str(exc))
    if len(candidate.effects) != 1:
        return _rejection_decision(
            candidate,
            receipt,
            "the current commitment slice requires exactly one effect",
        )

    effect = candidate.effects[0]
    handler = BUILTIN_EFFECTS.get(effect.effect_type)
    if handler is None:
        return _rejection_decision(
            candidate, receipt, f"unsupported effect type {effect.effect_type!r}"
        )
    required_capability, projector = handler
    if required_capability not in _actor_capabilities(candidate, genesis):
        return _rejection_decision(
            candidate,
            receipt,
            f"actor lacks required capability {required_capability!r}",
        )
    try:
        projection = projector(effect, candidate, receipt.id)
    except (TypeError, ValueError) as exc:
        return _rejection_decision(candidate, receipt, str(exc))
    capabilities = _actor_capabilities(candidate, genesis)
    missing_capabilities = tuple(
        capability
        for capability in projection.additional_capabilities
        if capability not in capabilities
    )
    if missing_capabilities:
        return _rejection_decision(
            candidate,
            receipt,
            f"actor lacks required capabilities {missing_capabilities!r}",
        )
    committed = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [record.id for record in projection.records],
        },
        causal_parents=(receipt.id, *(record.id for record in projection.records)),
    )
    trace = _candidate_trace(
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
                    "effect_type": effect.effect_type,
                    "relation_target": projection.relation_target,
                },
                sort_keys=True,
            )
        ],
    )
    return CommitmentDecision(
        candidate_id=candidate.id,
        accepted=True,
        runtime_records=(receipt, *projection.records, committed, _trace_record(trace)),
        trace_entries=(trace,),
        contract=projection.contract,
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
            candidate, genesis, verifier_factory=verifier_factory
        )
        if decision.assets:
            from aigineering.core.fact_reducer import FactReducer

            events = FactReducer(self._store, self._trace).on_assets_created(
                decision.assets
            )
            reducer_traces, reducer_records = materialize_fact_reduction(
                events, decision.assets
            )
            decision = replace(
                decision,
                trace_entries=decision.trace_entries + tuple(reducer_traces),
                runtime_records=decision.runtime_records
                + reducer_records
                + trace_records(reducer_traces),
            )
        self._store.commit_ingress_batch(
            accepted_assets=list(decision.assets),
            trace_entries=list(decision.trace_entries),
            contract=decision.contract,
            runtime_records=decision.runtime_records,
        )
        if self._trace is not self._store:
            for entry in decision.trace_entries:
                self._trace.append(entry)
        return decision
