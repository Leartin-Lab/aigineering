"""Operational candidate submission for the worker protocol.

Provides a one-shot submit path that handles projection, asset commit,
trace recording, and idempotency in a single call — suitable for
standalone worker environments.
"""

from __future__ import annotations

import json
from typing import Optional

from aigineering.core.disclosure import compute_disclosure
from aigineering.core.idempotency_store import IdempotencyStore
from aigineering.core.ids import now_iso
from aigineering.core.projection import project_candidate
from aigineering.core.provenance import sign_asset
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import TraceStoreProtocol, create_entry
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Candidate, Contract, ProjectionResult
from aigineering.protocol.wire import asset_to_dict, contract_to_dict, trace_entry_to_dict


class SubmitConflictError(Exception):
    """Raised when a different idempotency key has already been used for this contract."""


def submit_candidate(
    envelope: CandidateEnvelope,
    store: StoreProtocol,
    trace_store: TraceStoreProtocol,
    idempotency_store: IdempotencyStore | None = None,
    idempotency_key: str = "",
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

    # ── Idempotency ──────────────────────────────────────────────────
    if idempotency_key:
        cached = idem.get(contract.id, idempotency_key)
        if cached is not None:
            result = dict(cached)
            result["duplicate"] = True
            return result

        if idem.has_any(contract.id):
            raise SubmitConflictError(
                f"Contract '{contract.id}' already has a submission with a "
                f"different idempotency key"
            )

    # ── Disclosure scope ─────────────────────────────────────────────
    scope = compute_disclosure(contract, store)
    scope_ids = [a.id for a in scope]

    # ── Build Candidate ──────────────────────────────────────────────
    candidate = Candidate(
        worker_id=envelope.worker_id,
        raw_output=envelope.raw_output,
    )

    # ── Projection (commitment boundary) ─────────────────────────────
    result: ProjectionResult = project_candidate(contract, candidate)

    # ── Commit accepted assets ───────────────────────────────────────
    for asset in result.accepted_assets:
        store.add_asset(sign_asset(asset))

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
    trace_store.append(entry)

    # ── Build response ───────────────────────────────────────────────
    response: dict = {
        "contract_id": contract.id,
        "status": result.status.value,
        "accepted_assets": [asset_to_dict(a) for a in result.accepted_assets],
        "rejected_candidates": rejected_dicts,
        "trace_id": entry.id,
        "duplicate": False,
    }

    # ── Store idempotency result ─────────────────────────────────────
    if idempotency_key:
        cached_result = {k: v for k, v in response.items() if k != "duplicate"}
        idem.set(contract.id, idempotency_key, cached_result)

    return response
