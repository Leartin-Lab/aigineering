"""FastAPI application for the Aigineering API server."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from aigineering.application import (
    find_trace_for_session as _find_trace_for_session,
    latest_session_file as _latest_session_file,
    persistent_store as _persistent_store,
)
from aigineering.runtime import (
    WorkerSubmissionCommitError,
    claim_next_package,
    submit_worker_proposal,
)

from aigineering.core.commitment import CandidateCommitter
from aigineering.core.worker_coordination import authenticate_worker_command
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    replacement_claim_effect,
)
from aigineering.protocol.immutability import deep_thaw

app = FastAPI(title="Aigineering API", version="0.5.0")


# ── Request / response models ────────────────────────────────────────────────


class CandidateEffectRequest(BaseModel):
    effect_type: str
    payload: dict
    atomic_group: str = ""


class CandidateClaimBindingRequest(BaseModel):
    contract_id: str
    claim_id: str
    claim_epoch: int
    package_id: str


class CandidateProposalRequest(BaseModel):
    id: str
    domain_id: str
    actor_id: str
    key_id: str
    signature_kind: str
    signature: str
    effects: list[CandidateEffectRequest]
    causal_parents: list[str] = Field(default_factory=list)
    idempotency_key: str = ""
    claim_binding: CandidateClaimBindingRequest | None = None
    metadata: dict = Field(default_factory=dict)
    protocol_version: int = 1


class AssetSliceCandidateRequest(CandidateProposalRequest):
    range: str = ""


class AssetResponse(BaseModel):
    id: str
    name: str
    content: str
    content_type: str
    origin: str
    trust_tier: str
    promptable: bool
    definition_hash: str
    content_hash: str


class ContractResponse(BaseModel):
    id: str
    name: str
    inputs: list[str]
    outputs: list[str]
    activation: str
    budget: int
    labels: list[str]
    tool_scope: list[str]


class SessionResponse(BaseModel):
    id: str
    root_contract_id: str
    contract_ids: list[str]
    asset_ids: list[str]
    trace_ids: list[str]
    created_at: str


class TraceEntryResponse(BaseModel):
    id: str
    contract_id: str
    event_type: str
    authority_result: Optional[str] = None
    timestamp: str


class ReplacementClaimResponse(BaseModel):
    id: str
    source_asset_id: str
    replacement_asset_id: str
    definition_hash: str
    claim_type: str
    signed_by: str
    lineage_id: str


# ── Endpoints ────────────────────────────────────────────────────────────────


def _asset_response(asset) -> AssetResponse:
    return AssetResponse(
        id=asset.id,
        name=asset.name,
        content=asset.content,
        content_type=asset.content_type,
        origin=asset.origin,
        trust_tier=asset.trust_tier,
        promptable=asset.promptable,
        definition_hash=asset.definition_hash,
        content_hash=asset.content_hash,
    )


def _contract_response(contract) -> ContractResponse:
    return ContractResponse(
        id=contract.id,
        name=contract.name,
        inputs=list(contract.inputs),
        outputs=list(contract.outputs),
        activation=contract.activation,
        budget=contract.budget,
        labels=list(contract.labels),
        tool_scope=list(contract.tool_scope),
    )


def _trace_response(entry) -> TraceEntryResponse:
    return TraceEntryResponse(
        id=entry.id,
        contract_id=entry.contract_id,
        event_type=entry.event_type,
        authority_result=entry.authority_result,
        timestamp=entry.timestamp,
    )


def _replacement_claim_response(claim) -> ReplacementClaimResponse:
    return ReplacementClaimResponse(
        id=claim.id,
        source_asset_id=claim.source_asset_id,
        replacement_asset_id=claim.replacement_asset_id,
        definition_hash=claim.definition_hash,
        claim_type=claim.claim_type,
        signed_by=claim.signed_by,
        lineage_id=claim.lineage_id,
    )


def _request_store() -> Iterator:
    """Own exactly one operational Store connection for one HTTP request."""
    store = _persistent_store()
    try:
        yield store
    finally:
        store.close()


def _commit_candidate_request(body: CandidateProposalRequest, store):
    try:
        candidate = candidate_proposal_from_dict(body.model_dump())
        return CandidateCommitter(store, store).commit(candidate)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkerSubmissionCommitError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _rejection_reason(decision) -> str:
    for record in decision.runtime_records:
        if record.record_type.endswith("rejected"):
            return str(record.payload["reason"])
    return "Candidate rejected"


def _require_single_effect(body: CandidateProposalRequest, effect_type: str) -> None:
    if len(body.effects) != 1 or body.effects[0].effect_type != effect_type:
        raise HTTPException(
            status_code=422,
            detail=f"resource endpoint requires one {effect_type} effect",
        )


@app.post("/candidates")
def commit_candidate(body: CandidateProposalRequest, store=Depends(_request_store)):
    """Authenticate and commit one typed Candidate through the common boundary."""
    decision = _commit_candidate_request(body, store)
    return {
        "accepted": decision.accepted,
        "assets": [_asset_response(asset).model_dump() for asset in decision.assets],
        "candidate_id": decision.candidate_id,
        "contract": (
            _contract_response(decision.contract).model_dump()
            if decision.contract is not None
            else None
        ),
        "record_ids": [record.id for record in decision.runtime_records],
        "record_types": [record.record_type for record in decision.runtime_records],
        "rejection_reason": None if decision.accepted else _rejection_reason(decision),
    }


@app.post("/contracts", response_model=ContractResponse, status_code=201)
def create_contract(body: CandidateProposalRequest, store=Depends(_request_store)):
    """Commit exactly one signed ``contract.declare`` Candidate."""
    _require_single_effect(body, "contract.declare")
    decision = _commit_candidate_request(body, store)
    if not decision.accepted:
        raise HTTPException(status_code=422, detail=_rejection_reason(decision))
    if decision.contract is None or decision.assets:
        raise HTTPException(
            status_code=422,
            detail="POST /contracts requires one contract.declare effect",
        )

    return _contract_response(decision.contract)


@app.post("/assets", response_model=AssetResponse, status_code=201)
def create_asset(body: CandidateProposalRequest, store=Depends(_request_store)):
    """Commit exactly one signed ``asset.propose`` Candidate."""
    _require_single_effect(body, "asset.propose")
    decision = _commit_candidate_request(body, store)
    if not decision.accepted:
        raise HTTPException(status_code=422, detail=_rejection_reason(decision))
    if len(decision.assets) != 1 or decision.contract is not None:
        raise HTTPException(
            status_code=422,
            detail="POST /assets requires one asset.propose effect",
        )

    return _asset_response(decision.assets[0])


@app.get("/contracts", response_model=list[ContractResponse])
def list_contracts(store=Depends(_request_store)):
    """List contracts in the runtime store."""
    return [_contract_response(c) for c in store.get_all_contracts()]


@app.get("/assets", response_model=list[AssetResponse])
def list_assets(store=Depends(_request_store)):
    """List assets in the runtime store."""
    return [_asset_response(a) for a in store.get_all_assets()]


@app.get("/assets/{name}/versions", response_model=list[AssetResponse])
def get_asset_versions(name: str, store=Depends(_request_store)):
    """List all versions of an asset by name."""
    from aigineering.core.asset_versions import list_versions

    versions = list_versions(store, name)
    if not versions:
        raise HTTPException(status_code=404, detail=f"No asset named '{name}'")
    return [_asset_response(a) for a in versions]


@app.post("/assets/{name}/slice", response_model=AssetResponse, status_code=201)
def slice_asset(
    name: str,
    body: AssetSliceCandidateRequest,
    store=Depends(_request_store),
):
    """Create a new asset from a line or character slice of an existing asset."""
    from aigineering.core.asset_versions import create_slice_asset, resolve_latest

    source = resolve_latest(store, name)
    if source is None:
        raise HTTPException(status_code=404, detail=f"No asset named '{name}'")
    _require_single_effect(body, "asset.propose")
    proposed = body.effects[0].payload.get("asset")
    if not isinstance(proposed, dict) or not proposed.get("name"):
        raise HTTPException(status_code=422, detail="slice requires asset.name")
    try:
        expected = create_slice_asset(
            source,
            slice_name=str(proposed["name"]),
            range_spec=body.range,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    expected_payload = deep_thaw(asset_proposal_effect(expected).payload)
    if body.effects[0].payload != expected_payload:
        raise HTTPException(
            status_code=422,
            detail="signed asset.propose payload does not match the requested slice",
        )
    decision = _commit_candidate_request(body, store)
    if not decision.accepted:
        raise HTTPException(status_code=403, detail=_rejection_reason(decision))
    return _asset_response(decision.assets[0])


@app.post(
    "/replacement-claims",
    response_model=ReplacementClaimResponse,
    status_code=201,
)
def create_replacement_claim(
    body: CandidateProposalRequest, store=Depends(_request_store)
):
    """Create a replacement/slice/summary/redaction claim between two assets."""
    from aigineering.core.asset_versions import (
        create_replacement_claim as make_replacement_claim,
    )

    _require_single_effect(body, "asset.relate")
    proposed = body.effects[0].payload.get("claim")
    if not isinstance(proposed, dict):
        raise HTTPException(status_code=422, detail="asset.relate requires claim")
    source = store.get_asset(str(proposed.get("source_asset_id", "")))
    if source is None:
        raise HTTPException(status_code=404, detail="Source asset not found")
    replacement = store.get_asset(str(proposed.get("replacement_asset_id", "")))
    if replacement is None:
        raise HTTPException(status_code=404, detail="Replacement asset not found")
    try:
        claim = make_replacement_claim(
            source_asset_id=source.id,
            replacement_asset_id=replacement.id,
            definition_hash=source.definition_hash,
            claim_type=str(proposed.get("claim_type", "replacement")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    expected_payload = deep_thaw(replacement_claim_effect(claim).payload)
    if body.effects[0].payload != expected_payload:
        raise HTTPException(
            status_code=422,
            detail="signed asset.relate payload does not match stored assets",
        )
    decision = _commit_candidate_request(body, store)
    if not decision.accepted:
        raise HTTPException(status_code=403, detail=_rejection_reason(decision))
    committed_claim = next(
        (item for item in store.get_claims_for_asset(source.id) if item.id == claim.id),
        None,
    )
    if committed_claim is None:
        raise HTTPException(
            status_code=500,
            detail="Committed replacement claim is missing from the read model",
        )
    return _replacement_claim_response(committed_claim)


@app.get("/replacement-claims", response_model=list[ReplacementClaimResponse])
def list_replacement_claims(
    definition_hash: Optional[str] = Query(None),
    source_asset_id: Optional[str] = Query(None),
    store=Depends(_request_store),
):
    """List replacement claims by definition hash or source asset."""
    claims = []
    seen: set[str] = set()

    if definition_hash:
        for claim in store.get_claims_by_definition(definition_hash):
            claims.append(claim)
            seen.add(claim.id)
    if source_asset_id:
        for claim in store.get_claims_for_asset(source_asset_id):
            if claim.id not in seen:
                claims.append(claim)
                seen.add(claim.id)
    if not definition_hash and not source_asset_id:
        for asset in store.get_all_assets():
            for claim in store.get_claims_for_asset(asset.id):
                if claim.id not in seen:
                    claims.append(claim)
                    seen.add(claim.id)
            if asset.definition_hash:
                for claim in store.get_claims_by_definition(asset.definition_hash):
                    if claim.id not in seen:
                        claims.append(claim)
                        seen.add(claim.id)

    return [_replacement_claim_response(c) for c in claims]


@app.get("/contracts/{contract_id}", response_model=ContractResponse)
def get_contract(contract_id: str, store=Depends(_request_store)):
    """Get a contract by ID."""
    contract = store.get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")

    return _contract_response(contract)


@app.post("/worker/claims")
def claim_worker_package(body: CandidateProposalRequest, store=Depends(_request_store)):
    """Authenticate and atomically claim one disclosure-bound worker package."""
    try:
        command = authenticate_worker_command(
            candidate_proposal_from_dict(body.model_dump()), "worker.claim", store
        )
        payload = command.payload
        worker_id = str(payload.get("worker_id", ""))
        contract_id = payload.get("contract_id")
        if contract_id is not None and not isinstance(contract_id, str):
            raise ValueError("worker.claim contract_id must be a string or null")
        lease_seconds = int(payload.get("lease_seconds", 60))
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        claimed = claim_next_package(
            store,
            worker_id=worker_id,
            contract_id=contract_id,
            lease_seconds=lease_seconds,
            claim_runtime_records=command.runtime_records,
        )
    except (LookupError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if claimed is None:
        raise HTTPException(status_code=409, detail="No eligible contract available")
    return json.loads(claimed.package.to_json())


@app.post("/worker/claims/{claim_id}/renew")
def renew_worker_claim(
    claim_id: str,
    body: CandidateProposalRequest,
    store=Depends(_request_store),
):
    """Authenticate and renew one fenced claim on any replica."""
    try:
        command = authenticate_worker_command(
            candidate_proposal_from_dict(body.model_dump()),
            "worker.claim.renew",
            store,
        )
        payload = command.payload
        if str(payload.get("claim_id", "")) != claim_id:
            raise ValueError("renewal Candidate claim_id does not match request path")
        claim_epoch = int(payload.get("claim_epoch", 0))
        lease_seconds = int(payload.get("lease_seconds", 60))
        if lease_seconds < 1 or claim_epoch < 1:
            raise ValueError("lease_seconds and claim_epoch must be positive")
        renewed = store.renew_claim(
            claim_id,
            claim_epoch,
            str(payload.get("worker_id", "")),
            lease_seconds=lease_seconds,
            runtime_records=command.runtime_records,
        )
    except (LookupError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if renewed is None:
        raise HTTPException(status_code=409, detail="Claim renewal was rejected")
    return renewed


@app.post("/worker/submissions")
def submit_worker_candidate(
    body: CandidateProposalRequest, store=Depends(_request_store)
):
    """Commit a signed and claim-fenced worker Candidate on any replica."""
    try:
        proposal = candidate_proposal_from_dict(body.model_dump())
        return submit_worker_proposal(
            proposal,
            store,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/contracts/{contract_id}/run", status_code=410)
def run_contract(contract_id: str):
    """Reject the removed server-side worker impersonation endpoint."""
    del contract_id
    raise HTTPException(
        status_code=410,
        detail="Use /worker/claims and signed /worker/submissions",
    )


@app.get("/trace", response_model=list[TraceEntryResponse])
def get_trace(
    session_id: Optional[str] = Query(None),
    store=Depends(_request_store),
):
    """Get trace entries, optionally filtered by session ID."""
    if session_id:
        _, entries = _find_trace_for_session(session_id)
        if entries is None:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        from aigineering.core.trace import JsonLTraceStore

        entries = store.get_all()
        if entries:
            return [_trace_response(e) for e in entries]

        latest = _latest_session_file()
        if latest is None:
            return []
        store = JsonLTraceStore(str(latest))
        entries = store.get_all()

    return [_trace_response(e) for e in (entries or [])]


@app.get("/sessions", response_model=list[SessionResponse])
def get_sessions():
    """List all sessions."""
    from aigineering.core.session import SessionStore

    session_store = SessionStore()
    sessions = session_store.list_sessions()

    return [
        SessionResponse(
            id=s.id,
            root_contract_id=s.root_contract_id,
            contract_ids=list(s.contract_ids),
            asset_ids=list(s.asset_ids),
            trace_ids=list(s.trace_ids),
            created_at=s.created_at,
        )
        for s in sessions
    ]


@app.get("/assets/{name}", response_model=list[AssetResponse])
def get_assets(name: str, store=Depends(_request_store)):
    """Get assets by name."""
    assets = store.get_assets_by_name(name)
    if not assets:
        raise HTTPException(status_code=404, detail=f"No asset named '{name}'")

    return [_asset_response(a) for a in assets]
