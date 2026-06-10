"""Canonical JSON helpers for protocol types."""

from __future__ import annotations

import json
from typing import Any

from aigineering.protocol.types import Asset, Candidate, Contract, Session, TraceEntry


def asset_to_canonical(asset: Asset) -> str:
    d = {
        "name": asset.name,
        "content": asset.content,
        "content_type": asset.content_type,
        "created_by": asset.created_by,
        "origin": asset.origin,
    }
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def asset_to_dict(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "name": asset.name,
        "content": asset.content,
        "content_type": asset.content_type,
        "created_by": asset.created_by,
        "origin": asset.origin,
    }


def contract_to_canonical(contract: Contract) -> str:
    d = {
        "parent_id": contract.parent_id,
        "name": contract.name,
        "description": contract.description,
        "inputs": sorted(contract.inputs),
        "outputs": sorted(contract.outputs),
        "activation": contract.activation,
        "budget": contract.budget,
        "tool_scope": sorted(contract.tool_scope),
        "labels": sorted(contract.labels),
        "origin": contract.origin,
    }
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def contract_to_dict(contract: Contract) -> dict[str, Any]:
    return {
        "id": contract.id,
        "parent_id": contract.parent_id,
        "name": contract.name,
        "description": contract.description,
        "inputs": contract.inputs,
        "outputs": contract.outputs,
        "activation": contract.activation,
        "budget": contract.budget,
        "tool_scope": contract.tool_scope,
        "labels": contract.labels,
        "origin": contract.origin,
    }


def trace_entry_to_dict(entry: TraceEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "parent_id": entry.parent_id,
        "contract_id": entry.contract_id,
        "event_type": entry.event_type,
        "disclosed_assets": entry.disclosed_assets,
        "worker_id": entry.worker_id,
        "candidate_raw": entry.candidate_raw,
        "accepted_fragments": entry.accepted_fragments,
        "accepted_asset_names": entry.accepted_asset_names,
        "rejected_fragments": entry.rejected_fragments,
        "authority_policy": entry.authority_policy,
        "authority_result": entry.authority_result,
        "budget_remaining": entry.budget_remaining,
        "relation_type": entry.relation_type,
        "relation_target": entry.relation_target,
        "timestamp": entry.timestamp,
    }


def trace_entry_from_dict(data: dict[str, Any]) -> TraceEntry:
    return TraceEntry(
        id=data.get("id", ""),
        parent_id=data.get("parent_id"),
        contract_id=data.get("contract_id", ""),
        event_type=data.get("event_type", ""),
        disclosed_assets=data.get("disclosed_assets", []),
        worker_id=data.get("worker_id"),
        candidate_raw=data.get("candidate_raw"),
        accepted_fragments=data.get("accepted_fragments", []),
        accepted_asset_names=data.get("accepted_asset_names", []),
        rejected_fragments=data.get("rejected_fragments", []),
        authority_policy=data.get("authority_policy"),
        authority_result=data.get("authority_result"),
        budget_remaining=data.get("budget_remaining", 0),
        relation_type=data.get("relation_type"),
        relation_target=data.get("relation_target"),
        timestamp=data.get("timestamp", ""),
    )


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "worker_id": candidate.worker_id,
        "raw_output": candidate.raw_output,
        "parsed_action": candidate.parsed_action,
    }


def session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "root_contract_id": session.root_contract_id,
        "contract_ids": session.contract_ids,
        "asset_ids": session.asset_ids,
        "trace_ids": session.trace_ids,
        "config_snapshot": session.config_snapshot,
        "worker_snapshot": session.worker_snapshot,
        "created_at": session.created_at,
    }


def session_from_dict(data: dict[str, Any]) -> Session:
    return Session(
        id=data.get("id", ""),
        root_contract_id=data.get("root_contract_id", ""),
        contract_ids=data.get("contract_ids", []),
        asset_ids=data.get("asset_ids", []),
        trace_ids=data.get("trace_ids", []),
        config_snapshot=data.get("config_snapshot", {}),
        worker_snapshot=data.get("worker_snapshot", {}),
        created_at=data.get("created_at", ""),
    )
