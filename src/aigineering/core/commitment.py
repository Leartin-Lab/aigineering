"""Pure Candidate decision and transactional commitment boundary.

This module initially supports only the ``contract.declare`` vertical slice.
Unsupported or invalid effects are rejected as a whole and recorded visibly;
there is no fallback to direct RuntimeIngress writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, TYPE_CHECKING

from aigineering.core.activation import validate_execution_activation
from aigineering.core.authority import matched_reserved_prefix
from aigineering.core.ids import validate_contract_identity
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.fact_materialization import (
    asset_committed_record,
    materialize_fact_reduction,
    trace_records,
)
from aigineering.core.provenance import sign_asset
from aigineering.core.trace import create_entry
from aigineering.core.signing import create_verifier
from aigineering.protocol.candidate import (
    CandidateProposal,
    GenesisManifest,
    VerifierFactory,
    candidate_received_record,
    validate_genesis_manifest,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Asset, Contract, TraceEntry
from aigineering.protocol.wire import contract_to_dict, trace_entry_to_dict

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol


CONTRACT_DECLARE_CAPABILITY = "contract.publish"
ASSET_PROPOSE_CAPABILITY = "asset.publish"


@dataclass(frozen=True)
class CommitmentDecision:
    """Pure result of reducing one authenticated Candidate."""

    candidate_id: str
    accepted: bool
    runtime_records: tuple[RuntimeRecord, ...]
    trace_entries: tuple[TraceEntry, ...]
    contract: Contract | None = None
    assets: tuple[Asset, ...] = ()


def _contract_from_payload(payload: Mapping[str, Any]) -> Contract:
    contract_value = payload.get("contract")
    if not isinstance(contract_value, Mapping):
        raise ValueError("contract.declare requires an object payload.contract")
    data = deep_thaw(contract_value)
    return Contract(
        id=str(data.get("id", "")),
        parent_id=data.get("parent_id"),
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        inputs=tuple(data.get("inputs", ())),
        outputs=tuple(data.get("outputs", ())),
        activation=str(data.get("activation", "")),
        budget=int(data.get("budget", 0)),
        tool_scope=tuple(data.get("tool_scope", ())),
        labels=tuple(data.get("labels", ())),
        worker_capabilities=tuple(data.get("worker_capabilities", ())),
        worker_pools=tuple(data.get("worker_pools", ())),
        origin=str(data.get("origin", "human")),
        minting_authority=tuple(data.get("minting_authority", ())),
        sensitive_input_policy=data.get("sensitive_input_policy"),
    )


def _asset_from_payload(
    payload: Mapping[str, Any], candidate: CandidateProposal
) -> Asset:
    asset_value = payload.get("asset")
    if not isinstance(asset_value, Mapping):
        raise ValueError("asset.propose requires an object payload.asset")
    data = deep_thaw(asset_value)
    name = str(data.get("name", ""))
    content = str(data.get("content", ""))
    if not name:
        raise ValueError("asset.propose asset.name must not be empty")
    prefix = matched_reserved_prefix(name)
    if prefix is not None:
        raise ValueError(f"Asset name {name!r} uses protected prefix {prefix!r}")
    content_hash = hash_asset_content(name, content)
    asset = Asset(
        id=content_hash,
        name=name,
        content=content,
        content_type=str(data.get("content_type", "text")),
        created_by=candidate.actor_id,
        origin=str(data.get("origin", "human")),
        trust_tier=str(data.get("trust_tier", "human")),
        source_uri=str(data.get("source_uri", "")),
        signed_by=candidate.actor_id,
        signer_kind=f"candidate:{candidate.signature_kind}",
        promptable=bool(data.get("promptable", True)),
        disclosure_view=str(data.get("disclosure_view", "original")),
        definition_hash=hash_asset_definition(name),
        content_hash=content_hash,
    )
    return sign_asset(asset, signed_by=candidate.actor_id)


def validate_contract_commitment(
    contract: Contract, *, require_canonical_v3: bool = True
) -> None:
    """Apply Contract admission rules without touching a Store."""
    if require_canonical_v3 and not contract.id.startswith("task:v3:"):
        raise ValueError("Candidate contracts require a canonical task:v3 identity")
    validate_contract_identity(contract)
    validate_execution_activation(contract.activation)
    for output_name in contract.outputs:
        prefix = matched_reserved_prefix(output_name)
        if prefix is not None and output_name not in contract.minting_authority:
            raise ValueError(
                f"Contract output {output_name!r} uses protected prefix {prefix!r} "
                "without minting authority"
            )


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
    capabilities = _actor_capabilities(candidate, genesis)
    if effect.effect_type == "asset.propose":
        if ASSET_PROPOSE_CAPABILITY not in capabilities:
            return _rejection_decision(
                candidate,
                receipt,
                f"actor lacks required capability {ASSET_PROPOSE_CAPABILITY!r}",
            )
        try:
            asset = _asset_from_payload(effect.payload, candidate)
        except (TypeError, ValueError) as exc:
            return _rejection_decision(candidate, receipt, str(exc))
        committed_asset = asset_committed_record(asset, causal_parents=(receipt.id,))
        committed = create_runtime_record(
            "candidate.committed",
            {
                "candidate_id": candidate.id,
                "committed_record_ids": [committed_asset.id],
            },
            causal_parents=(receipt.id, committed_asset.id),
        )
        trace = _candidate_trace(
            contract_id="commitment",
            event_type="candidate_committed",
            parent_id=candidate.id,
            worker_id=candidate.actor_id,
            relation_target=asset.id,
            authority_result="accepted",
            accepted_asset_names=[asset.name],
            accepted_fragments=[
                json.dumps(
                    {
                        "asset_id": asset.id,
                        "candidate_id": candidate.id,
                        "effect_type": effect.effect_type,
                    },
                    sort_keys=True,
                )
            ],
        )
        return CommitmentDecision(
            candidate_id=candidate.id,
            accepted=True,
            runtime_records=(
                receipt,
                committed_asset,
                committed,
                _trace_record(trace),
            ),
            trace_entries=(trace,),
            assets=(asset,),
        )

    if effect.effect_type != "contract.declare":
        return _rejection_decision(
            candidate, receipt, f"unsupported effect type {effect.effect_type!r}"
        )
    if CONTRACT_DECLARE_CAPABILITY not in capabilities:
        return _rejection_decision(
            candidate,
            receipt,
            f"actor lacks required capability {CONTRACT_DECLARE_CAPABILITY!r}",
        )

    try:
        contract = _contract_from_payload(effect.payload)
        validate_contract_commitment(contract)
    except (TypeError, ValueError) as exc:
        return _rejection_decision(candidate, receipt, str(exc))

    declared = create_runtime_record(
        "contract.declared",
        {
            "candidate_id": candidate.id,
            "contract": contract_to_dict(contract),
        },
        causal_parents=(receipt.id,),
    )
    committed = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": [declared.id],
        },
        causal_parents=(receipt.id, declared.id),
    )
    trace = _candidate_trace(
        contract_id="commitment",
        event_type="candidate_committed",
        parent_id=candidate.id,
        worker_id=candidate.actor_id,
        relation_target=contract.id,
        authority_result="accepted",
        accepted_fragments=[
            json.dumps(
                {
                    "candidate_id": candidate.id,
                    "contract_id": contract.id,
                    "effect_type": effect.effect_type,
                },
                sort_keys=True,
            )
        ],
    )
    return CommitmentDecision(
        candidate_id=candidate.id,
        accepted=True,
        runtime_records=(receipt, declared, committed, _trace_record(trace)),
        trace_entries=(trace,),
        contract=contract,
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
        already_recorded = any(
            (
                record.record_type == "candidate.received"
                and record.payload["candidate_id"] == candidate.id
            )
            or (
                record.record_type == "candidate.authentication_rejected"
                and record.payload["claimed_candidate_id"] == candidate.id
            )
            for _, record in self._store.scan_runtime_records()
        )
        if already_recorded:
            return decision
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
