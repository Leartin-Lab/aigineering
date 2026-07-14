"""FastAPI application for the Aigineering API server."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from aigineering.cli._common import (
    _find_trace_for_session,
    _persistent_store,
)
from aigineering.cli.worker_runtime import (
    claim_next_package,
    execute_claimed_package,
)

from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.submit import (
    SubmitClaimError,
    SubmitCommitError,
    SubmitConflictError,
    submit_candidate,
)
from aigineering.protocol.envelope import CandidateEnvelope

app = FastAPI(title="Aigineering API", version="0.5.0")


# ── Request / response models ────────────────────────────────────────────────


class ContractCreateRequest(BaseModel):
    name: str
    inputs: list[str] = []
    outputs: list[str] = []
    activation: str = ""
    budget: int = 5
    labels: list[str] = []
    tool_scope: list[str] = []
    description: str = ""


class ContractRunRequest(BaseModel):
    worker: str = "mock"
    output_content: str = ""


class WorkerClaimRequest(BaseModel):
    worker_id: str
    contract_id: str | None = None
    lease_seconds: int = 60


class WorkerRenewRequest(BaseModel):
    worker_id: str
    claim_epoch: int
    lease_seconds: int = 60


class WorkerSubmitRequest(BaseModel):
    contract_id: str
    worker_id: str
    raw_output: str
    package_id: str
    claim_id: str
    claim_epoch: int
    idempotency_key: str = ""
    parsed_action: dict | None = None


class AssetCreateRequest(BaseModel):
    name: str
    content: str
    origin: str = "human"
    trust_tier: str = "human"
    source_uri: str = ""
    promptable: bool = True
    content_type: str = "text"


class AssetSliceRequest(BaseModel):
    slice_name: str
    range: str = ""


class ReplacementClaimCreateRequest(BaseModel):
    source_asset_id: str
    replacement_asset_id: str
    claim_type: str = "replacement"
    signed_by: str = ""


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


class ContractRunResponse(BaseModel):
    contract_id: str
    status: str
    trace_ids: list[str]
    output_asset_ids: list[str]


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


@app.post("/contracts", response_model=ContractResponse, status_code=201)
def create_contract(body: ContractCreateRequest):
    """Inject a new contract into the runtime store."""
    from aigineering.core.control_plane import inject_contract

    store = _persistent_store()
    ingress = RuntimeIngress(store, store)
    try:
        contract = inject_contract(
            store,
            store,
            name=body.name,
            inputs=tuple(body.inputs),
            outputs=tuple(body.outputs),
            activation=body.activation,
            budget=body.budget,
            labels=tuple(body.labels),
            tool_scope=tuple(body.tool_scope),
            description=body.description,
            ingress=ingress,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return _contract_response(contract)


@app.post("/assets", response_model=AssetResponse, status_code=201)
def create_asset(body: AssetCreateRequest):
    """Inject a new asset through the control-plane API."""
    from aigineering.core.control_plane import inject_asset

    store = _persistent_store()
    ingress = RuntimeIngress(store, store)
    try:
        asset = inject_asset(
            store,
            store,
            name=body.name,
            content=body.content,
            origin=body.origin,
            trust_tier=body.trust_tier,
            source_uri=body.source_uri,
            promptable=body.promptable,
            content_type=body.content_type,
            ingress=ingress,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return _asset_response(asset)


@app.get("/contracts", response_model=list[ContractResponse])
def list_contracts():
    """List contracts in the runtime store."""
    store = _persistent_store()
    return [_contract_response(c) for c in store.get_all_contracts()]


@app.get("/assets", response_model=list[AssetResponse])
def list_assets():
    """List assets in the runtime store."""
    store = _persistent_store()
    return [_asset_response(a) for a in store.get_all_assets()]


@app.get("/assets/{name}/versions", response_model=list[AssetResponse])
def get_asset_versions(name: str):
    """List all versions of an asset by name."""
    from aigineering.core.asset_versions import list_versions

    store = _persistent_store()
    versions = list_versions(store, name)
    if not versions:
        raise HTTPException(status_code=404, detail=f"No asset named '{name}'")
    return [_asset_response(a) for a in versions]


@app.post("/assets/{name}/slice", response_model=AssetResponse, status_code=201)
def slice_asset(name: str, body: AssetSliceRequest):
    """Create a new asset from a line or character slice of an existing asset."""
    from aigineering.core.asset_versions import create_slice_asset, resolve_latest

    store = _persistent_store()
    source = resolve_latest(store, name)
    if source is None:
        raise HTTPException(status_code=404, detail=f"No asset named '{name}'")
    try:
        asset = create_slice_asset(
            source,
            slice_name=body.slice_name,
            range_spec=body.range,
        )
        ingress = RuntimeIngress(store, store)
        ingress.accept_asset(asset, source="asset_slice")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _asset_response(asset)


@app.post(
    "/replacement-claims",
    response_model=ReplacementClaimResponse,
    status_code=201,
)
def create_replacement_claim(body: ReplacementClaimCreateRequest):
    """Create a replacement/slice/summary/redaction claim between two assets."""
    from aigineering.core.asset_versions import (
        create_replacement_claim as make_replacement_claim,
    )

    store = _persistent_store()
    source = store.get_asset(body.source_asset_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source asset not found")
    replacement = store.get_asset(body.replacement_asset_id)
    if replacement is None:
        raise HTTPException(status_code=404, detail="Replacement asset not found")
    try:
        claim = make_replacement_claim(
            source_asset_id=source.id,
            replacement_asset_id=replacement.id,
            definition_hash=source.definition_hash,
            claim_type=body.claim_type,
            signed_by=body.signed_by,
        )
        ingress = RuntimeIngress(store, store)
        ingress.accept_replacement_claim(claim, source="server")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _replacement_claim_response(claim)


@app.get("/replacement-claims", response_model=list[ReplacementClaimResponse])
def list_replacement_claims(
    definition_hash: Optional[str] = Query(None),
    source_asset_id: Optional[str] = Query(None),
):
    """List replacement claims by definition hash or source asset."""
    store = _persistent_store()
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
def get_contract(contract_id: str):
    """Get a contract by ID."""
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")

    return _contract_response(contract)


@app.post("/worker/claims")
def claim_worker_package(body: WorkerClaimRequest):
    """Atomically claim one contract and return its disclosure-bound package."""
    if body.lease_seconds < 1:
        raise HTTPException(status_code=400, detail="lease_seconds must be positive")
    store = _persistent_store()
    try:
        claimed = claim_next_package(
            store,
            worker_id=body.worker_id,
            contract_id=body.contract_id,
            lease_seconds=body.lease_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if claimed is None:
        raise HTTPException(status_code=409, detail="No eligible contract available")
    return json.loads(claimed.package.to_json())


@app.post("/worker/claims/{claim_id}/renew")
def renew_worker_claim(claim_id: str, body: WorkerRenewRequest):
    """Renew a fenced claim; any replica may service the request."""
    if body.lease_seconds < 1 or body.claim_epoch < 1:
        raise HTTPException(
            status_code=400,
            detail="lease_seconds and claim_epoch must be positive",
        )
    store = _persistent_store()
    renewed = store.renew_claim(
        claim_id,
        body.claim_epoch,
        body.worker_id,
        lease_seconds=body.lease_seconds,
    )
    if renewed is None:
        raise HTTPException(status_code=409, detail="Claim renewal was rejected")
    return renewed


@app.post("/worker/submissions")
def submit_worker_candidate(body: WorkerSubmitRequest):
    """Commit a fenced candidate; any replica may service the request."""
    if body.parsed_action is not None:
        raise HTTPException(
            status_code=422,
            detail="Method actions require the method submission protocol",
        )
    try:
        envelope = CandidateEnvelope(**body.model_dump())
        store = _persistent_store()
        result = submit_candidate(
            envelope,
            store,
            store,
            RuntimeIngress(store, store),
            idempotency_key=envelope.idempotency_key,
        )
        if result["status"] == "rejected":
            from aigineering.cli.worker_runtime import process_rejected_submissions

            process_rejected_submissions(store)
        return result
    except (ValueError, SubmitClaimError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SubmitConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SubmitCommitError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/contracts/{contract_id}/run", response_model=ContractRunResponse)
def run_contract(contract_id: str, body: ContractRunRequest):
    """Claim and run one contract through the worker protocol."""
    if body.worker != "mock":
        raise HTTPException(status_code=400, detail="Only mock worker is supported")

    from aigineering.agent.mock import MockWorker

    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")

    output_content = body.output_content or f"API output for {contract.name}"
    raw_output = "\n".join(
        f"{output_name}: {output_content}" for output_name in contract.outputs
    )
    worker = MockWorker()
    worker.set_output(contract.name, raw_output)
    try:
        claimed = claim_next_package(
            store,
            worker_id="server:mock",
            contract_id=contract.id,
        )
        if claimed is None:
            raise HTTPException(
                status_code=409,
                detail="Contract is not enabled or is already claimed/terminal",
            )
        result = execute_claimed_package(claimed, worker, store)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    entries = store.get_by_contract(contract.id)
    outputs = store.get_assets_by_contract(contract.id)
    status = "complete" if result.get("complete") is True else result["status"]
    return ContractRunResponse(
        contract_id=contract.id,
        status=status,
        trace_ids=[entry.id for entry in entries],
        output_asset_ids=[asset.id for asset in outputs],
    )


@app.get("/trace", response_model=list[TraceEntryResponse])
def get_trace(
    session_id: Optional[str] = Query(None),
):
    """Get trace entries, optionally filtered by session ID."""
    if session_id:
        _, entries = _find_trace_for_session(session_id)
        if entries is None:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        from aigineering.cli._common import _latest_session_file
        from aigineering.core.trace import JsonLTraceStore

        store = _persistent_store()
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
def get_assets(name: str):
    """Get assets by name."""
    store = _persistent_store()
    assets = store.get_assets_by_name(name)
    if not assets:
        raise HTTPException(status_code=404, detail=f"No asset named '{name}'")

    return [_asset_response(a) for a in assets]
