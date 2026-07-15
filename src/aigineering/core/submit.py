"""Operational candidate submission for the worker protocol.

Provides a one-shot submit path that handles projection, asset commit,
trace recording, and idempotency in a single call — suitable for
standalone worker environments.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.commitment import record_candidate_rejection
from aigineering.core.disclosure import DisclosurePolicyError, compute_disclosure
from aigineering.core.domain import load_genesis
from aigineering.core.fact_materialization import reduce_asset_facts
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.projection import project_candidate
from aigineering.core.provenance import sign_asset
from aigineering.core.store import StoreProtocol, require_operational_store
from aigineering.core.trace import TraceStoreProtocol, create_entry
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.candidate import CandidateProposal, candidate_received_record
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Asset, Candidate, Contract, ProjectionResult
from aigineering.protocol.wire import asset_to_dict, trace_entry_to_dict


class SubmitConflictError(Exception):
    """Raised when a different idempotency key has already been used for this contract."""


class SubmitClaimError(Exception):
    """Raised when envelope.claim_id does not match an active claim for the contract."""


class SubmitCommitError(Exception):
    """Raised when an atomic submission commit cannot be completed safely."""


@dataclass(frozen=True)
class WorkerCandidateAuthentication:
    """Verified identity carried across projection into the Store transaction."""

    candidate: CandidateProposal
    receipt: RuntimeRecord

    @property
    def candidate_id(self) -> str:
        return self.candidate.id

    @property
    def key_id(self) -> str:
        return self.candidate.key_id


def replay_idempotent_submission(
    store,
    *,
    contract_id: str,
    idempotency_key: str,
    candidate_hash: str,
) -> dict | None:
    """Return a safe duplicate response or reject conflicting reuse."""
    if not idempotency_key:
        return None
    cached = store.get_idempotency(contract_id, idempotency_key)
    if cached is not None:
        response = dict(cached)
        cached_candidate_hash = response.pop("_candidate_hash", "")
        if cached_candidate_hash and cached_candidate_hash != candidate_hash:
            raise SubmitConflictError(
                f"Idempotency key for contract '{contract_id}' is already "
                "bound to a different Candidate payload"
            )
        response["duplicate"] = True
        return response
    if store.has_any_idempotency(contract_id):
        raise SubmitConflictError(
            f"Contract '{contract_id}' already has a submission with a "
            "different idempotency key"
        )
    return None


def validate_submission_claim(store, contract: Contract, envelope: CandidateEnvelope):
    """Validate identity, epoch, lease and package fencing for any action."""
    claim = store.get_claim(contract.id)
    if not envelope.claim_id:
        raise SubmitClaimError(
            f"Contract '{contract.id}' requires claim-bound submission; "
            "use worker next before submitting"
        )
    if claim is None:
        raise SubmitClaimError(
            f"No active claim for contract '{contract.id}' — "
            f"worker '{envelope.worker_id}' must claim before submitting"
        )
    if claim.get("claim_id") != envelope.claim_id:
        raise SubmitClaimError(
            f"claim_id mismatch: envelope='{envelope.claim_id}' "
            f"vs store='{claim.get('claim_id')}'"
        )
    if envelope.claim_epoch < 1:
        raise SubmitClaimError(
            "claim-bound envelope.claim_epoch is required and must be positive"
        )
    if claim.get("epoch") != envelope.claim_epoch:
        raise SubmitClaimError(
            f"claim epoch mismatch: envelope={envelope.claim_epoch} "
            f"vs store={claim.get('epoch')}"
        )
    if claim.get("worker_id") != envelope.worker_id:
        raise SubmitClaimError(
            f"claim owned by '{claim.get('worker_id')}', not '{envelope.worker_id}'"
        )
    if claim.get("status") != "active":
        raise SubmitClaimError(
            f"claim status is '{claim.get('status')}', not 'active' — "
            "retry/recovery must create a new contract before submitting"
        )
    lease_until = claim.get("lease_until", "")
    from datetime import datetime, timezone

    if not lease_until:
        raise SubmitClaimError(
            "active claim has no lease_until — "
            "refusing to accept claim with unbounded lease"
        )
    try:
        lease_dt = datetime.fromisoformat(lease_until)
    except ValueError as e:
        raise SubmitClaimError(
            f"claim lease_until is malformed ({lease_until!r}): {e} — "
            "refusing to accept unparseable lease timestamp"
        )
    if lease_dt.tzinfo is None:
        lease_dt = lease_dt.replace(tzinfo=timezone.utc)
    if lease_dt < datetime.now(timezone.utc):
        raise SubmitClaimError(
            f"claim lease expired at {lease_until} — "
            "retry/recovery must create a new contract before submitting"
        )
    if claim.get("package_id") and not envelope.package_id:
        raise SubmitClaimError(
            "active claim is bound to a package_id; envelope.package_id is required"
        )
    if claim.get("package_id") and claim.get("package_id") != envelope.package_id:
        raise SubmitClaimError(
            f"package_id mismatch: envelope='{envelope.package_id}' "
            f"vs claim='{claim.get('package_id')}'"
        )
    return claim


def submit_candidate(
    envelope: CandidateEnvelope,
    store: StoreProtocol,
    trace_store: TraceStoreProtocol,
    idempotency_store: object | None = None,
    idempotency_key: str = "",
    authentication: WorkerCandidateAuthentication | None = None,
) -> dict:
    """Process a candidate envelope through the commitment boundary.

    Returns a JSON-serializable result dict with schema::

        {
            "contract_id": str,
            "status": "accepted" | "rejected" | "partial",
            "accepted_assets": [{"id": str, "name": str, ...}],
            "rejected_candidates": [{"name": str, "reason": str, "category": str}],
            "trace_id": str,
            "duplicate": bool,
        }

    Raises *SubmitConflictError* when a different idempotency key was
    already used for the same contract.
    """
    operational = require_operational_store(store)
    if idempotency_store is not None:
        raise TypeError(
            "external idempotency stores are not supported; idempotency must "
            "commit atomically with the Candidate"
        )
    contract = operational.get_contract(envelope.contract_id)
    if contract is None:
        raise ValueError(f"Contract '{envelope.contract_id}' not found in store")

    effective_idempotency_key = idempotency_key or envelope.idempotency_key
    effective_candidate_id = (
        authentication.candidate_id if authentication else envelope.candidate_hash
    )

    # ── Idempotency ──────────────────────────────────────────────────
    duplicate = replay_idempotent_submission(
        operational,
        contract_id=contract.id,
        idempotency_key=effective_idempotency_key,
        candidate_hash=effective_candidate_id,
    )
    if duplicate is not None:
        return duplicate

    # ── Claim validation (G8) ────────────────────────────────────────
    # Every operational submission is fenced by a durable claim.
    validate_submission_claim(operational, contract, envelope)

    # ── Disclosure scope ─────────────────────────────────────────────
    scope = compute_disclosure(contract, store)
    scope_ids = [a.id for a in scope]

    # ── Build Candidate ──────────────────────────────────────────────
    candidate = Candidate(
        worker_id=envelope.worker_id,
        raw_output=envelope.raw_output,
        parsed_action=envelope.parsed_action,
        metadata=envelope.usage_metadata,
    )

    # ── Projection (commitment boundary) ─────────────────────────────
    projection_result: ProjectionResult = project_candidate(contract, candidate)

    signed_assets = [sign_asset(asset) for asset in projection_result.accepted_assets]

    # ── Build rejection dicts ────────────────────────────────────────
    rejected_dicts = [
        {
            "name": r.name,
            "content": r.content,
            "reject_reason": r.reject_reason,
            "category": r.category.value,
        }
        for r in projection_result.rejected_candidates
    ]

    # ── Trace entry ──────────────────────────────────────────────────
    # Determine sequence from existing entries for this contract.
    existing = trace_store.get_by_contract(contract.id)
    seq = len(existing)

    entry = create_entry(
        contract_id=contract.id,
        event_type="projection",
        sequence=seq,
        disclosed_assets=scope_ids,
        worker_id=candidate.worker_id,
        candidate_raw=candidate.raw_output,
        accepted_fragments=[a.id for a in projection_result.accepted_assets],
        accepted_asset_names=[a.name for a in projection_result.accepted_assets],
        rejected_fragments=[
            f"[{r['category']}] {r['name']}: {r['reject_reason']}"
            for r in rejected_dicts
        ],
        authority_result=projection_result.status.value,
        authority_policy=(
            json.dumps(dict(projection_result.authority_policy), sort_keys=True)
            if projection_result.authority_policy is not None
            else None
        ),
        budget_remaining=contract.budget,
        usage_metadata=envelope.usage_metadata,
    )
    # ── Build response ───────────────────────────────────────────────
    response: dict = {
        "contract_id": contract.id,
        "status": projection_result.status.value,
        "accepted_assets": [asset_to_dict(a) for a in signed_assets],
        "rejected_candidates": rejected_dicts,
        "trace_id": entry.id,
        "duplicate": False,
    }

    # ── Completion check ────────────────────────────────────────────
    projected_output_names = {a.name for a in signed_assets}
    if _all_outputs_satisfied(
        contract, store, extra_output_names=projected_output_names
    ):
        response["complete"] = True

    budget_entry = create_entry(
        contract_id=contract.id,
        event_type="budget_consumed",
        sequence=seq + 1,
        relation_type="worker_submit",
        budget_remaining=max(0, contract.budget - 1),
    )

    reducer_traces, reducer_records = reduce_asset_facts(
        store, trace_store, signed_assets
    )
    trace_entries = [entry, budget_entry, *reducer_traces]
    if response.get("complete") is True:
        complete_entry = next(
            (
                item
                for item in reducer_traces
                if item.contract_id == contract.id and item.event_type == "complete"
            ),
            None,
        )
        if complete_entry is not None:
            response["complete_trace_id"] = complete_entry.id

    runtime_records = _submission_runtime_records(
        envelope=envelope,
        projection_result=projection_result,
        signed_assets=signed_assets,
        rejected_dicts=rejected_dicts,
        budget_remaining=max(0, contract.budget - 1),
        authentication=authentication,
    )
    runtime_records = (
        *runtime_records,
        *reducer_records,
        *(
            create_runtime_record(
                "trace.recorded",
                {"trace": trace_entry_to_dict(trace_entry)},
            )
            for trace_entry in trace_entries
        ),
    )

    cached_result = {k: v for k, v in response.items() if k != "duplicate"}
    cached_result["_candidate_hash"] = effective_candidate_id
    try:
        committed = operational.commit_candidate_submission(
            signed_assets,
            trace_entries,
            effective_idempotency_key,
            cached_result,
            envelope.claim_id,
            envelope.worker_id,
            envelope.package_id,
            envelope.claim_epoch,
            runtime_records=runtime_records,
            candidate_key_id=authentication.key_id if authentication else "",
        )
    except (sqlite3.Error, ImmutableRecordConflict, ValueError) as e:
        raise SubmitCommitError(
            f"submission could not be atomically committed: {e}"
        ) from e
    if committed is False:
        raise SubmitCommitError(
            f"claim '{envelope.claim_id}' could not be atomically submitted"
        )

    return response


def submit_worker_candidate(
    candidate: CandidateProposal,
    store: StoreProtocol,
    trace_store: TraceStoreProtocol,
) -> dict:
    """Authenticate and commit one signed, claim-bound worker output Candidate."""
    operational = require_operational_store(store)
    envelope, authentication = authenticate_worker_candidate(
        candidate, operational, trace_store
    )
    return submit_authenticated_worker_candidate(
        envelope, authentication, operational, trace_store
    )


def submit_authenticated_worker_candidate(
    envelope: CandidateEnvelope,
    authentication: WorkerCandidateAuthentication,
    store: StoreProtocol,
    trace_store: TraceStoreProtocol,
) -> dict:
    """Commit output after a reusable WorkerHost authentication decision."""
    operational = require_operational_store(store)
    try:
        return submit_candidate(
            envelope,
            operational,
            trace_store,
            idempotency_key=authentication.candidate.idempotency_key,
            authentication=authentication,
        )
    except (
        DisclosurePolicyError,
        SubmitClaimError,
        SubmitCommitError,
        SubmitConflictError,
        TypeError,
        ValueError,
    ) as exc:
        record_candidate_rejection(
            authentication.candidate,
            str(exc),
            operational,
            trace_store,
            receipt=authentication.receipt,
        )
        raise


def authenticate_worker_candidate(
    candidate: CandidateProposal,
    store,
    trace_store,
) -> tuple[CandidateEnvelope, WorkerCandidateAuthentication]:
    """Verify actor and worker semantics without committing proposed output."""
    operational = require_operational_store(store)
    genesis = load_genesis(operational)
    actor_keys = load_effective_actor_keys(operational, genesis)
    try:
        receipt = candidate_received_record(
            candidate,
            genesis,
            actor_keys=actor_keys,
        )
    except ValueError as exc:
        record_candidate_rejection(candidate, str(exc), operational, trace_store)
        raise
    try:
        envelope = _worker_candidate_envelope(candidate, actor_keys, operational)
    except (TypeError, ValueError) as exc:
        record_candidate_rejection(
            candidate,
            str(exc),
            operational,
            trace_store,
            receipt=receipt,
        )
        raise
    return envelope, WorkerCandidateAuthentication(candidate, receipt)


def _worker_candidate_envelope(candidate, actor_keys, store) -> CandidateEnvelope:
    """Parse an authenticated worker effect and enforce routing identity."""
    if len(candidate.effects) != 1 or candidate.effects[0].effect_type not in {
        "task.delegate",
        "worker.output",
    }:
        raise ValueError(
            "worker submission requires exactly one 'worker.output' or "
            "'task.delegate' effect"
        )
    actor_key = next(
        key
        for key in actor_keys
        if key.actor_id == candidate.actor_id and key.key_id == candidate.key_id
    )
    if "worker.submit" not in actor_key.capabilities:
        raise ValueError("worker actor key lacks required capability 'worker.submit'")
    payload = deep_thaw(candidate.effects[0].payload)
    envelope_data = payload.get("envelope")
    if not isinstance(envelope_data, Mapping):
        raise ValueError(
            f"{candidate.effects[0].effect_type} effect requires an envelope object"
        )
    envelope = CandidateEnvelope.from_dict(envelope_data)
    if candidate.actor_id != envelope.worker_id:
        raise ValueError("Candidate actor_id must equal envelope.worker_id")
    if not candidate.idempotency_key:
        raise ValueError("signed worker Candidate requires an idempotency_key")
    if candidate.idempotency_key != envelope.idempotency_key:
        raise ValueError(
            "Candidate and worker envelope idempotency keys must be identical"
        )
    registration = store.get_worker_registration(envelope.worker_id)
    if registration is None or not registration.enabled:
        raise ValueError(f"worker '{envelope.worker_id}' is not enabled and registered")
    if (registration.actor_id, registration.key_id) != (
        candidate.actor_id,
        candidate.key_id,
    ):
        raise ValueError("Candidate actor key does not match worker registration")
    return envelope


def _submission_runtime_records(
    *,
    envelope: CandidateEnvelope,
    projection_result: ProjectionResult,
    signed_assets: list[Asset],
    rejected_dicts: list[dict],
    budget_remaining: int,
    authentication: WorkerCandidateAuthentication | None = None,
) -> tuple[RuntimeRecord, ...]:
    """Build the immutable causal facts for one candidate commitment."""
    candidate_id = (
        authentication.candidate_id if authentication else envelope.candidate_hash
    )
    receipt = (
        authentication.receipt
        if authentication
        else create_runtime_record(
            "candidate.received",
            {
                "candidate_id": candidate_id,
                "claim_epoch": envelope.claim_epoch,
                "claim_id": envelope.claim_id,
                "contract_id": envelope.contract_id,
                "idempotency_key": envelope.idempotency_key,
                "package_id": envelope.package_id,
                "parsed_action": envelope.parsed_action,
                "protocol_version": envelope.protocol_version,
                "raw_output": envelope.raw_output,
                "worker_id": envelope.worker_id,
                "usage_metadata": envelope.usage_metadata,
            },
        )
    )
    projection_parent = receipt
    records: list[RuntimeRecord] = [receipt]
    if authentication is not None:
        output = create_runtime_record(
            "worker.output.received",
            {
                "candidate_id": candidate_id,
                "claim_epoch": envelope.claim_epoch,
                "claim_id": envelope.claim_id,
                "contract_id": envelope.contract_id,
                "idempotency_key": envelope.idempotency_key,
                "package_id": envelope.package_id,
                "parsed_action": envelope.parsed_action,
                "protocol_version": envelope.protocol_version,
                "raw_output": envelope.raw_output,
                "usage_metadata": envelope.usage_metadata,
                "worker_id": envelope.worker_id,
            },
            causal_parents=[receipt.id],
        )
        records.append(output)
        projection_parent = output
    projection = create_runtime_record(
        "projection.decided",
        {
            "accepted_asset_ids": [asset.id for asset in signed_assets],
            "authority_policy": (
                dict(projection_result.authority_policy)
                if projection_result.authority_policy is not None
                else None
            ),
            "candidate_id": candidate_id,
            "contract_id": envelope.contract_id,
            "rejections": rejected_dicts,
            "status": projection_result.status.value,
        },
        causal_parents=[projection_parent.id],
    )
    records.append(projection)
    if projection_result.status.value == "rejected":
        records.append(
            create_runtime_record(
                "lifecycle.terminal",
                {"contract_id": envelope.contract_id, "terminal": "failed"},
                causal_parents=[projection.id],
            )
        )
    records.extend(
        create_runtime_record(
            "asset.committed",
            {"asset": asset_to_dict(asset), "contract_id": envelope.contract_id},
            causal_parents=[projection.id],
        )
        for asset in signed_assets
    )
    budget = create_runtime_record(
        "budget.consumed",
        {
            "amount": 1,
            "contract_id": envelope.contract_id,
            "remaining": budget_remaining,
        },
        causal_parents=[projection.id],
    )
    records.append(budget)
    return tuple(records)


def _all_outputs_satisfied(
    contract: Contract,
    store: StoreProtocol,
    extra_output_names: set[str] | None = None,
) -> bool:
    """Return True when declared outputs exist and are valid output facts."""
    return all_outputs_satisfied(
        contract,
        store,
        extra_output_names=extra_output_names,
    )
