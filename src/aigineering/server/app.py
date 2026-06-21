"""FastAPI application for the Aigineering API server."""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from aigineering.cli._common import (
    _find_trace_for_session,
    _persistent_store,
)

app = FastAPI(title="Aigineering API", version="0.5.0-alpha.1")


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


class AssetCreateRequest(BaseModel):
    name: str
    content: str
    origin: str = "human"
    trust_tier: str = "human"
    source_uri: str = ""
    promptable: bool = True
    content_type: str = "text"


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


# ── Endpoints ────────────────────────────────────────────────────────────────


@app.post("/contracts", response_model=ContractResponse, status_code=201)
def create_contract(body: ContractCreateRequest):
    """Inject a new contract into the runtime store."""
    from aigineering.core.control_plane import inject_contract

    store = _persistent_store()
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
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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


@app.post("/assets", response_model=AssetResponse, status_code=201)
def create_asset(body: AssetCreateRequest):
    """Inject a new asset through the control-plane API."""
    from aigineering.core.control_plane import inject_asset

    store = _persistent_store()
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
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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


@app.get("/contracts/{contract_id}", response_model=ContractResponse)
def get_contract(contract_id: str):
    """Get a contract by ID."""
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")

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

        latest = _latest_session_file()
        if latest is None:
            return []
        store = JsonLTraceStore(str(latest))
        entries = store.get_all()

    return [
        TraceEntryResponse(
            id=e.id,
            contract_id=e.contract_id,
            event_type=e.event_type,
            authority_result=e.authority_result,
            timestamp=e.timestamp,
        )
        for e in (entries or [])
    ]


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

    return [
        AssetResponse(
            id=a.id,
            name=a.name,
            content=a.content,
            content_type=a.content_type,
            origin=a.origin,
            trust_tier=a.trust_tier,
            promptable=a.promptable,
            definition_hash=a.definition_hash,
            content_hash=a.content_hash,
        )
        for a in assets
    ]
