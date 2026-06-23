"""Operational candidate submission for the worker protocol.

Provides a one-shot submit path that handles projection, asset commit,
trace recording, and idempotency in a single call — suitable for
standalone worker environments.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.core.disclosure import compute_disclosure
from aigineering.core.idempotency_store import IdempotencyStore
from aigineering.core.projection import project_candidate
from aigineering.core.provenance import sign_asset
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import TraceStoreProtocol, create_entry
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Candidate, Contract, ProjectionResult
from aigineering.protocol.wire import asset_to_dict

if TYPE_CHECKING:
    from aigineering.core.runtime_ingress import RuntimeIngress


class SubmitConflictError(Exception):
    """Raised when a different idempotency key has already been used for this contract."""


class SubmitClaimError(Exception):
    """Raised when envelope.claim_id does not match an active claim for the contract."""


class SubmitCommitError(Exception):
    """Raised when an atomic submission commit cannot be completed safely."""


def submit_candidate(
    envelope: CandidateEnvelope,
    store: StoreProtocol,
    trace_store: TraceStoreProtocol,
    idempotency_store: IdempotencyStore | None = None,
    idempotency_key: str = "",
    ingress: RuntimeIngress | None = None,
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
    contract = store.get_contract(envelope.contract_id)
    if contract is None:
        raise ValueError(f"Contract '{envelope.contract_id}' not found in store")

    idem = idempotency_store if idempotency_store is not None else IdempotencyStore()
    effective_idempotency_key = idempotency_key or envelope.idempotency_key

    # ── Idempotency ──────────────────────────────────────────────────
    if effective_idempotency_key:
        store_get_idem = getattr(store, "get_idempotency", None)
        cached = (
            store_get_idem(contract.id, effective_idempotency_key)
            if store_get_idem is not None
            else idem.get(contract.id, effective_idempotency_key)
        )
        if cached is not None:
            result = dict(cached)
            result["duplicate"] = True
            return result

        store_has_any = getattr(store, "has_any_idempotency", None)
        has_any = (
            store_has_any(contract.id)
            if store_has_any is not None
            else idem.has_any(contract.id)
        )
        if has_any:
            raise SubmitConflictError(
                f"Contract '{contract.id}' already has a submission with a "
                f"different idempotency key"
            )

    # ── Claim validation (G8) ────────────────────────────────────────
    # If the envelope carries a claim_id, verify it matches an active
    # claim for this contract owned by this worker, and that the lease
    # has not expired. Stores that don't track claims (e.g. MemoryStore)
    # skip this check — only SQLite-backed stores enforce it.
    get_claim = getattr(store, "get_claim", None)
    active_claim = get_claim(contract.id) if get_claim is not None else None
    requires_claim = getattr(store, "commit_candidate_submission", None) is not None
    if requires_claim and not envelope.claim_id:
        raise SubmitClaimError(
            f"Contract '{contract.id}' requires claim-bound submission; "
            "use worker next before submitting"
        )
    if (
        active_claim is not None
        and active_claim.get("status") == "active"
        and not envelope.claim_id
    ):
        raise SubmitClaimError(
            f"Contract '{contract.id}' has an active claim; envelope.claim_id is required"
        )

    if envelope.claim_id:
        if get_claim is not None:
            claim = active_claim if active_claim is not None else get_claim(contract.id)
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
            if claim.get("worker_id") != envelope.worker_id:
                raise SubmitClaimError(
                    f"claim owned by '{claim.get('worker_id')}', "
                    f"not '{envelope.worker_id}'"
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
                    f"refusing to accept unparseable lease timestamp"
                )
            now = datetime.now(timezone.utc)
            if lease_dt.tzinfo is None:
                lease_dt = lease_dt.replace(tzinfo=timezone.utc)
            if lease_dt < now:
                raise SubmitClaimError(
                    f"claim lease expired at {lease_until} — "
                    "retry/recovery must create a new contract before submitting"
                )
            if claim.get("package_id") and not envelope.package_id:
                raise SubmitClaimError(
                    "active claim is bound to a package_id; envelope.package_id is required"
                )
            if (
                claim.get("package_id")
                and claim.get("package_id") != envelope.package_id
            ):
                raise SubmitClaimError(
                    f"package_id mismatch: envelope='{envelope.package_id}' "
                    f"vs claim='{claim.get('package_id')}'"
                )

    # ── Disclosure scope ─────────────────────────────────────────────
    scope = compute_disclosure(contract, store)
    scope_ids = [a.id for a in scope]

    # ── Build Candidate ──────────────────────────────────────────────
    candidate = Candidate(
        worker_id=envelope.worker_id,
        raw_output=envelope.raw_output,
        parsed_action=envelope.parsed_action,
    )

    # ── Projection (commitment boundary) ─────────────────────────────
    result: ProjectionResult = project_candidate(contract, candidate)

    signed_assets = [sign_asset(asset) for asset in result.accepted_assets]

    # ── Build rejection dicts ────────────────────────────────────────
    rejected_dicts = [
        {
            "name": r.name,
            "content": r.content,
            "reject_reason": r.reject_reason,
            "category": r.category.value,
        }
        for r in result.rejected_candidates
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
        accepted_fragments=[a.id for a in result.accepted_assets],
        accepted_asset_names=[a.name for a in result.accepted_assets],
        rejected_fragments=[
            f"[{r['category']}] {r['name']}: {r['reject_reason']}"
            for r in rejected_dicts
        ],
        authority_result=result.status.value,
        authority_policy=(
            json.dumps(dict(result.authority_policy), sort_keys=True)
            if result.authority_policy is not None
            else None
        ),
        budget_remaining=contract.budget,
    )
    # ── Build response ───────────────────────────────────────────────
    response: dict = {
        "contract_id": contract.id,
        "status": result.status.value,
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

    trace_entries = [entry, budget_entry]
    if response.get("complete") is True:
        complete_entry = create_entry(
            contract_id=contract.id,
            event_type="complete",
            sequence=seq + 2,
            budget_remaining=max(0, contract.budget - 1),
        )
        response["complete_trace_id"] = complete_entry.id
        trace_entries.append(complete_entry)

    cached_result = {k: v for k, v in response.items() if k != "duplicate"}
    commit_submission = getattr(store, "commit_candidate_submission", None)
    if commit_submission is not None:
        try:
            committed = commit_submission(
                signed_assets,
                trace_entries,
                effective_idempotency_key,
                cached_result,
                envelope.claim_id,
                envelope.worker_id,
                envelope.package_id,
            )
        except Exception as e:
            raise SubmitCommitError(
                f"submission could not be atomically committed: {e}"
            ) from e
        if committed is False:
            raise SubmitCommitError(
                f"claim '{envelope.claim_id}' could not be atomically submitted"
            )
    else:
        if ingress is not None:
            for asset in signed_assets:
                ingress.accept_asset(asset, source="candidate")
        else:
            for asset in signed_assets:
                store.add_asset(asset)
        for trace_entry in trace_entries:
            trace_store.append(trace_entry)
        if effective_idempotency_key:
            idem.set(contract.id, effective_idempotency_key, cached_result)

    return response


def _all_outputs_satisfied(
    contract: Contract,
    store: StoreProtocol,
    extra_output_names: set[str] | None = None,
) -> bool:
    """Return True when all declared contract outputs exist in the store."""
    extra_output_names = extra_output_names or set()
    for output_name in contract.outputs:
        if output_name in extra_output_names:
            continue
        if not store.get_assets_by_name(output_name):
            return False
    return True
