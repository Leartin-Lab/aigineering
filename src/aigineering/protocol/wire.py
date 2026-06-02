"""Canonical JSON helpers for protocol types."""

from __future__ import annotations

import json
from typing import Any

from aigineering.protocol.types import Asset, Candidate, Contract, TraceEntry


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
        "rejected_fragments": entry.rejected_fragments,
        "authority_policy": entry.authority_policy,
        "authority_result": entry.authority_result,
        "budget_remaining": entry.budget_remaining,
        "relation_type": entry.relation_type,
        "relation_target": entry.relation_target,
        "timestamp": entry.timestamp,
    }


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "worker_id": candidate.worker_id,
        "raw_output": candidate.raw_output,
        "parsed_action": candidate.parsed_action,
    }
