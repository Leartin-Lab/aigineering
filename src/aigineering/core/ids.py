"""Typed canonical hashing for content-addressed runtime objects (v0.3.1b)."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

from aigineering.protocol.immutability import deep_thaw

CONTRACT_SELF_REFERENCE = "{contract_id}"

# ---------------------------------------------------------------------------
# Canonical JSON serialization (RFC 8785 – style)
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Serialize *obj* to a deterministic JSON string with stable key ordering.

    Rules:
      - Keys sorted alphabetically
      - No whitespace (compact separators)
      - Unicode preserved (ensure_ascii=False)
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_content_hash(content: str) -> str:
    """SHA-256 of *content* after Unicode NFC normalization.

    Normalization guarantees that canonically equivalent representations
    (e.g. U+00E9 vs U+0065 + U+0301) produce the same hash.
    """
    normalized = unicodedata.normalize("NFC", content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Legacy helpers (preserved for minimal call-site churn)
# ---------------------------------------------------------------------------


def hash_content(content: str) -> str:
    """Legacy alias for *compute_content_hash*."""
    return compute_content_hash(content)


# ---------------------------------------------------------------------------
# Typed hash domains
# ---------------------------------------------------------------------------


def hash_contract(
    name: str,
    description: str,
    inputs: list[str],
    outputs: list[str],
    activation: str,
    budget: int,
    tool_scope: list[str],
    labels: list[str],
    origin: str,
    worker_capabilities: list[str] | None = None,
    worker_pools: list[str] | None = None,
) -> str:
    """Deterministic contract identity with ``task:`` domain tag.

    Canonical fields (alphabetical in JSON):
      activation, budget, description, inputs, labels, name, origin,
      outputs, tool_scope, worker_capabilities, worker_pools

    ``parent_id`` is intentionally excluded – contracts are content-addressed
    independent of structural position.
    """
    fields: dict[str, object] = {
        "activation": activation,
        "budget": budget,
        "description": description,
        "inputs": sorted(inputs),
        "labels": sorted(labels),
        "name": name,
        "origin": origin,
        "outputs": sorted(outputs),
        "tool_scope": sorted(tool_scope),
    }
    if worker_capabilities:
        fields["worker_capabilities"] = sorted(worker_capabilities)
    if worker_pools:
        fields["worker_pools"] = sorted(worker_pools)
    canonical = canonical_json(fields)
    return f"task:{compute_content_hash(canonical)}"


def hash_contract_v2(
    name: str,
    description: str,
    inputs: list[str],
    outputs: list[str],
    activation: str,
    budget: int,
    tool_scope: list[str],
    labels: list[str],
    origin: str,
    parent_id: str | None = None,
    worker_capabilities: list[str] | tuple[str, ...] | None = None,
    worker_pools: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Deterministic contract identity **v2** with ``task:`` domain tag.

    Unlike :func:`hash_contract`, this function includes ``parent_id`` in
    the identity computation.  Two identical child definitions under
    different parents will produce different contract IDs.

    This is required by ADR-002 and the 050 runtime boundary plan.
    Legacy ``hash_contract`` is preserved for backward compatibility.
    """
    fields: dict[str, object] = {
        "activation": activation,
        "budget": budget,
        "description": description,
        "inputs": sorted(inputs),
        "labels": sorted(labels),
        "name": name,
        "origin": origin,
        "outputs": sorted(outputs),
        "tool_scope": sorted(tool_scope),
    }
    if parent_id is not None:
        fields["parent_id"] = parent_id
    if worker_capabilities:
        fields["worker_capabilities"] = sorted(worker_capabilities)
    if worker_pools:
        fields["worker_pools"] = sorted(worker_pools)
    canonical = canonical_json(fields)
    return f"task:{compute_content_hash(canonical)}"


def hash_contract_v3(
    *,
    name: str,
    description: str,
    inputs: list[str] | tuple[str, ...],
    outputs: list[str] | tuple[str, ...],
    activation: str,
    budget: int,
    tool_scope: list[str] | tuple[str, ...],
    labels: list[str] | tuple[str, ...],
    origin: str,
    parent_id: str | None = None,
    worker_capabilities: list[str] | tuple[str, ...] = (),
    worker_pools: list[str] | tuple[str, ...] = (),
    minting_authority: list[str] | tuple[str, ...] = (),
    sensitive_input_policy: dict[str, Any] | None = None,
) -> str:
    """Security-complete Contract identity with normalized self references."""
    fields: dict[str, object] = {
        "activation": activation,
        "budget": budget,
        "description": description,
        "inputs": sorted(inputs),
        "labels": sorted(labels),
        "minting_authority": sorted(minting_authority),
        "name": name,
        "origin": origin,
        "outputs": sorted(outputs),
        "parent_id": parent_id,
        "sensitive_input_policy": deep_thaw(sensitive_input_policy),
        "tool_scope": sorted(tool_scope),
        "worker_capabilities": sorted(worker_capabilities),
        "worker_pools": sorted(worker_pools),
    }
    return f"task:v3:{compute_content_hash(canonical_json(fields))}"


def contract_identity_v3(contract) -> str:
    """Recompute a v3 identity from one materialized Contract entity."""
    normalized_authority = [
        value.replace(contract.id, CONTRACT_SELF_REFERENCE)
        for value in contract.minting_authority
    ]
    policy = (
        deep_thaw(contract.sensitive_input_policy)
        if contract.sensitive_input_policy is not None
        else None
    )
    return hash_contract_v3(
        name=contract.name,
        description=contract.description,
        inputs=contract.inputs,
        outputs=contract.outputs,
        activation=contract.activation,
        budget=contract.budget,
        tool_scope=contract.tool_scope,
        labels=contract.labels,
        origin=contract.origin,
        parent_id=contract.parent_id,
        worker_capabilities=contract.worker_capabilities,
        worker_pools=contract.worker_pools,
        minting_authority=normalized_authority,
        sensitive_input_policy=policy,
    )


def validate_contract_identity(contract) -> None:
    """Fail closed when a v3 ID does not bind its complete effective entity."""
    if not contract.id.startswith("task:v3:"):
        return
    expected = contract_identity_v3(contract)
    if contract.id != expected:
        raise ValueError(
            f"Contract '{contract.id}' does not match canonical v3 identity "
            f"'{expected}'"
        )


def hash_asset_definition(name: str) -> str:
    """Identity of an asset *definition slot* (``def:`` tag).

    Only the asset *name* contributes – the definition slot is name-resolved.
    """
    return f"def:{compute_content_hash(name)}"


def hash_asset_content(name: str, content: str) -> str:
    """Identity of asset *content* (``content:`` tag).

    Both name and content contribute so that different assets with matching
    content are still distinguishable.
    """
    canonical = canonical_json({"name": name, "content": content})
    return f"content:{compute_content_hash(canonical)}"


def hash_lineage(group_name: str, asset_ids: list[str]) -> str:
    """Deterministic lineage group identity (``lineage:`` tag)."""
    canonical = canonical_json(
        {"group_name": group_name, "asset_ids": sorted(asset_ids)}
    )
    return f"lineage:{compute_content_hash(canonical)}"


def hash_event(
    contract_id: str,
    event_type: str,
    sequence: int = 0,
    parent_id: Optional[str] = None,
    payload: Any | None = None,
) -> str:
    """Deterministic trace-event identity (``event:`` tag).

    ``payload`` binds the effective event body when provided.  The optional
    argument preserves the legacy helper API while allowing real TraceEntry
    identities to cover candidate, authority, accepted/rejected effects and
    causal metadata instead of only a locally allocated sequence number.
    """
    components: dict[str, Any] = {
        "contract_id": contract_id,
        "event_type": event_type,
        "sequence": sequence,
    }
    if parent_id is not None:
        components["parent_id"] = parent_id
    if payload is not None:
        components["payload"] = payload
    canonical = canonical_json(components)
    return f"event:{compute_content_hash(canonical)}"


def hash_claim(
    source_id: str,
    replacement_id: str,
    claim_type: str,
) -> str:
    """Deterministic asset-replacement claim identity (``claim:`` tag)."""
    canonical = canonical_json(
        {
            "claim_type": claim_type,
            "replacement_id": replacement_id,
            "source_id": source_id,
        }
    )
    return f"claim:{compute_content_hash(canonical)}"


def hash_retry(original_contract_id: str) -> str:
    """Deterministic retry contract identity (``retry:`` tag).

    Derived from the original contract id so retrying the same contract
    always produces the same retry contract id.
    """
    return f"retry:{compute_content_hash(original_contract_id + ':retry')}"


# ---------------------------------------------------------------------------
# Convenience wrappers (backward-compatible signatures)
# ---------------------------------------------------------------------------


def asset_id(canonical_content: str) -> str:
    """Convenience wrapper – parse JSON and delegate to *hash_asset_content*."""
    try:
        data = json.loads(canonical_content)
    except json.JSONDecodeError:
        return f"asset:{compute_content_hash(canonical_content)}"
    name = str(data.get("name", ""))
    content = str(data.get("content", ""))
    return hash_asset_content(name, content)


def contract_id(canonical_content: str) -> str:
    """Convenience wrapper – parse JSON and delegate to *hash_contract*."""
    try:
        data = json.loads(canonical_content)
    except json.JSONDecodeError:
        return f"task:{compute_content_hash(canonical_content)}"
    return hash_contract(
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        inputs=_str_list(data.get("inputs", [])),
        outputs=_str_list(data.get("outputs", [])),
        activation=str(data.get("activation", "")),
        budget=_maybe_int(data.get("budget", 0), 0),
        tool_scope=_str_list(data.get("tool_scope", [])),
        labels=_str_list(data.get("labels", [])),
        origin=str(data.get("origin", "human")),
    )


def trace_entry_id(
    contract_id: str,
    event_type: str,
    sequence: int,
    parent_id: Optional[str] = None,
) -> str:
    """Backward-compatible wrapper for *hash_event*."""
    return hash_event(
        contract_id=contract_id,
        event_type=event_type,
        sequence=sequence,
        parent_id=parent_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _maybe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
