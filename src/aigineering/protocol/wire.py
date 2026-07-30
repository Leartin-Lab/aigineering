"""Canonical JSON helpers for protocol types."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset, Candidate, Contract, Session, TraceEntry


def asset_to_canonical(asset: Asset) -> str:
    """Canonical JSON for provenance seal computation.

    Note: ``signer_kind`` is intentionally excluded — it is metadata about
    the signer *mechanism*, not the asset content, and must not affect the
    provenance seal.
    """
    d = {
        "name": asset.name,
        "content": asset.content,
        "content_type": asset.content_type,
        "created_by": asset.created_by,
        "origin": asset.origin,
        "trust_tier": asset.trust_tier,
        "minted_by": asset.minted_by,
        "source_uri": asset.source_uri,
        "signed_by": asset.signed_by,
        "promptable": asset.promptable,
        "disclosure_view": asset.disclosure_view,
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
        "trust_tier": asset.trust_tier,
        "minted_by": asset.minted_by,
        "source_uri": asset.source_uri,
        "signed_by": asset.signed_by,
        "signer_kind": asset.signer_kind,
        "provenance_seal": asset.provenance_seal,
        "definition_hash": asset.definition_hash,
        "content_hash": asset.content_hash,
        "promptable": asset.promptable,
        "disclosure_view": asset.disclosure_view,
        "keep_flag": asset.keep_flag,
        "tombstoned": asset.tombstoned,
        "tombstoned_at": asset.tombstoned_at,
        "lineage_id": asset.lineage_id,
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
        "context_asset_ids": sorted(contract.context_asset_ids),
        "worker_capabilities": sorted(contract.worker_capabilities),
        "worker_pools": sorted(contract.worker_pools),
        "origin": contract.origin,
        "minting_authority": sorted(contract.minting_authority),
        "sensitive_input_policy": (
            deep_thaw(contract.sensitive_input_policy)
            if contract.sensitive_input_policy is not None
            else None
        ),
        "acceptance_policy": (
            deep_thaw(contract.acceptance_policy)
            if contract.acceptance_policy is not None
            else None
        ),
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
        "context_asset_ids": contract.context_asset_ids,
        "worker_capabilities": contract.worker_capabilities,
        "worker_pools": contract.worker_pools,
        "origin": contract.origin,
        "minting_authority": contract.minting_authority,
        "sensitive_input_policy": (
            deep_thaw(contract.sensitive_input_policy)
            if contract.sensitive_input_policy is not None
            else None
        ),
        "acceptance_policy": (
            deep_thaw(contract.acceptance_policy)
            if contract.acceptance_policy is not None
            else None
        ),
    }


def contract_from_dict(data: Mapping[str, Any]) -> Contract:
    """Rebuild a Contract from its public wire representation."""
    return Contract(**deep_thaw(dict(data)))


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
        "usage_metadata": (
            deep_thaw(entry.usage_metadata)
            if entry.usage_metadata is not None
            else None
        ),
    }


def trace_entry_from_dict(data: dict[str, Any]) -> TraceEntry:
    usage = data.get("usage_metadata")

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
        usage_metadata=(
            MappingProxyType(dict(usage)) if isinstance(usage, Mapping) else None
        ),
    )


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    parsed = candidate.parsed_action
    return {
        "worker_id": candidate.worker_id,
        "raw_output": candidate.raw_output,
        "parsed_action": deep_thaw(parsed) if parsed is not None else None,
    }


def session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "root_contract_id": session.root_contract_id,
        "contract_ids": session.contract_ids,
        "asset_ids": session.asset_ids,
        "trace_ids": session.trace_ids,
        "config_snapshot": deep_thaw(session.config_snapshot),
        "worker_snapshot": deep_thaw(session.worker_snapshot),
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
