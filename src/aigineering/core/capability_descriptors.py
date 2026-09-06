"""Capability descriptor assets for tools, MCPs, skills, memory, and personas.

Descriptors are Assets that carry origin, trust, and provenance metadata.
They describe what capabilities exist without exposing private configuration.
Private config (API keys, secrets) is sealed — only a reference hash is disclosed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aigineering.core.ids import (
    compute_content_hash,
    hash_asset_content,
    hash_asset_definition,
)
from aigineering.core.provenance import sign_asset, verify_asset_seal
from aigineering.core.trust_policy import TrustPolicy
from aigineering.protocol.types import Asset, TrustTier
from aigineering.protocol.immutability import deep_thaw

CAPABILITY_KINDS = ("tool", "mcp", "skill", "memory", "persona")

_NAME_PREFIX: dict[str, str] = {
    "tool": "_tool_capability_",
    "mcp": "_mcp_",
    "skill": "_skill_capability_",
    "memory": "_memory_capability_",
    "persona": "_persona_capability_",
    "provider": "_provider_config_",
}


def _build_descriptor_asset(
    kind: str,
    name: str,
    disclosed_content: dict[str, Any],
    trust_tier: str = "configured",
    source_uri: str = "",
) -> Asset:
    """Internal helper that builds a signed capability descriptor Asset.

    The disclosed_content dictionary is serialised as the Asset content.
    Private configuration (API keys, secrets) must NOT be present — only a
    sealed_config_ref reference is permitted.
    """
    asset_name = f"{_NAME_PREFIX[kind]}{name}"
    content_str = json.dumps(disclosed_content, sort_keys=True, ensure_ascii=False)
    c_hash = compute_content_hash(content_str)
    asset = Asset(
        id=f"cap:{c_hash}",
        name=asset_name,
        content=content_str,
        content_type="application/json",
        definition_hash=hash_asset_definition(asset_name),
        content_hash=hash_asset_content(asset_name, content_str),
        origin="capability_registry",
        trust_tier=trust_tier,
        minted_by="capability_registry",
        source_uri=source_uri,
    )
    return sign_asset(asset)


def create_tool_descriptor(
    name: str,
    description: str,
    input_schema: Mapping[str, Any],
    trust_tier: str = "configured",
    source_uri: str = "",
    *,
    output_schema: Mapping[str, Any] | None = None,
    version: str = "0.1.0",
    max_output_bytes: int = 1_048_576,
) -> Asset:
    """Create a capability descriptor Asset for a Tool.

    Parameters
    ----------
    name : str
        Tool name (e.g. ``"web_search"``).
    description : str
        Human-readable description of what the tool does.
    input_schema : Mapping
        JSON Schema describing the tool's input parameters.
    trust_tier : str
        Trust tier for the tool (default ``"configured"``).
    source_uri : str
        URI identifying the tool's origin (e.g. ``"tool://web_search"``).
    output_schema : Mapping or None
        JSON Schema describing the tool's JSON output, when applicable.
    version : str
        Tool contract version.
    max_output_bytes : int
        UTF-8 output limit bound into the signed descriptor.

    Returns
    -------
    Asset
        Signed descriptor Asset named ``_tool_capability_{name}``.
    """
    disclosed = {
        "kind": "tool",
        "name": name,
        "version": version,
        "description": description,
        "input_schema": deep_thaw(input_schema),
        "output_schema": deep_thaw(output_schema or {}),
        "max_output_bytes": max_output_bytes,
        "sealed_config_ref": "",
    }
    return _build_descriptor_asset("tool", name, disclosed, trust_tier, source_uri)


def create_mcp_descriptor(
    name: str,
    source_uri: str,
    trust_tier: str = "configured",
    tool_name: str = "",
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> Asset:
    """Create a capability descriptor Asset for an MCP (Model Context Protocol).

    The MCP descriptor carries origin and trust but does NOT expose private
    transport credentials or endpoint secrets — those are sealed.

    Parameters
    ----------
    name : str
        MCP server name (e.g. ``"filesystem"``).
    source_uri : str
        URI identifying the MCP endpoint (e.g. ``"mcp://filesystem"``).
    trust_tier : str
        Trust tier for the MCP (default ``"configured"``).
    tool_name : str
        Optional specific tool name within the MCP server
        (e.g. ``"search.query"``).  When empty, the descriptor covers
        the whole server.
    input_schema : dict or None
        Optional JSON Schema for the tool's input parameters.
    output_schema : dict or None
        Optional JSON Schema for the tool's output.

    Returns
    -------
    Asset
        Signed descriptor Asset named ``_mcp_{name}``.
    """
    disclosed: dict[str, Any] = {
        "kind": "mcp",
        "name": name,
        "version": "0.1.0",
        "source_uri": source_uri,
        "sealed_config_ref": "",
    }
    if tool_name:
        disclosed["tool_name"] = tool_name
    if input_schema is not None:
        disclosed["input_schema"] = input_schema
    if output_schema is not None:
        disclosed["output_schema"] = output_schema
    return _build_descriptor_asset("mcp", name, disclosed, trust_tier, source_uri)


def create_skill_descriptor(
    name: str,
    content: str,
    trust_tier: str = "configured",
) -> Asset:
    """Create a capability descriptor Asset for a Skill / procedure.

    Per ADR-005/007, the descriptor carries only **metadata** (kind, name,
    version, content hash, sealed ref).  The actual skill body is stored as
    a separate promptable asset (``_skill_content_{name}``) so that the
    descriptor does not mix capability proof with behaviour content.

    Parameters
    ----------
    name : str
        Skill name (e.g. ``"security_review"``).
    content : str
        The skill definition / procedure content (used to compute the hash only).
    trust_tier : str
        Trust tier for the skill (default ``"configured"``).

    Returns
    -------
    Asset
        Signed descriptor Asset named ``_skill_capability_{name}``.
    """
    disclosed = {
        "kind": "skill",
        "name": name,
        "version": "0.1.0",
        "source_uri": "",
        "content_hash": hash_asset_content(name, content),
        "sealed_config_ref": "",
    }
    return _build_descriptor_asset("skill", name, disclosed, trust_tier)


def create_memory_descriptor(
    name: str,
    source_uri: str,
    trust_tier: str = "configured",
) -> Asset:
    """Create a capability descriptor Asset for a Memory / context provider.

    Parameters
    ----------
    name : str
        Memory capability name (e.g. ``"session_context"``).
    source_uri : str
        URI identifying the memory source (e.g. ``"memory://session"``).
    trust_tier : str
        Trust tier for the memory capability (default ``"configured"``).

    Returns
    -------
    Asset
        Signed descriptor Asset named ``_memory_capability_{name}``.
    """
    disclosed = {
        "kind": "memory",
        "name": name,
        "version": "0.1.0",
        "source_uri": source_uri,
        "sealed_config_ref": "",
    }
    return _build_descriptor_asset("memory", name, disclosed, trust_tier, source_uri)


def create_persona_descriptor(
    name: str,
    content: str,
    trust_tier: str = "configured",
) -> Asset:
    """Create a capability descriptor Asset for a Persona / policy.

    Parameters
    ----------
    name : str
        Persona name (e.g. ``"auditor"``).
    content : str
        The persona definition / policy content.
    trust_tier : str
        Trust tier for the persona (default ``"configured"``).

    Returns
    -------
    Asset
        Signed descriptor Asset named ``_persona_capability_{name}``.
    """
    disclosed = {
        "kind": "persona",
        "name": name,
        "version": "0.1.0",
        "source_uri": "",
        "content_hash": hash_asset_content(name, content),
        "sealed_config_ref": "",
    }
    return _build_descriptor_asset("persona", name, disclosed, trust_tier)


def create_provider_config_snapshot(
    provider_name: str,
    base_url: str,
    model: str,
    timeout: float = 60.0,
    max_retries: int = 3,
    capabilities: tuple[str, ...] = (),
    trust_tier: str = "configured",
) -> Asset:
    """Create a traceable provider config snapshot asset.

    API keys are **never** included in the asset content.  The caller is
    expected to store the API key externally (environment variable, secret
    manager, …).

    Parameters
    ----------
    provider_name : str
        Logical provider name (e.g. ``"openai"``, ``"vllm_local"``).
    base_url : str
        Base URL of the chat-completions endpoint.
    model : str
        Model identifier (e.g. ``"gpt-4.1-mini"``).
    timeout : float
        Request timeout in seconds (default ``60.0``).
    max_retries : int
        Maximum retry count for transient errors (default ``3``).
    capabilities : tuple[str, ...]
        Provider capabilities (e.g. ``("json_schema", "tool_calling", "streaming")``).
    trust_tier : str
        Trust tier for the provider config (default ``"configured"``).

    Returns
    -------
    Asset
        Signed descriptor Asset named ``_provider_config_{provider_name}``.
    """
    disclosed_content: dict[str, Any] = {
        "provider_name": provider_name,
        "base_url": base_url,
        "model": model,
        "timeout": timeout,
        "max_retries": max_retries,
        "capabilities": sorted(capabilities),
        "sealed_config_ref": "",
    }

    return _build_descriptor_asset(
        "provider",
        provider_name,
        disclosed_content,
        trust_tier,
        f"provider://{provider_name}",
    )


# Minimum trust tier required for capability descriptors.
_MINIMUM_TRUST_TIER = "configured"


def verify_descriptor(
    descriptor: Asset, kind: str | None = None, policy: TrustPolicy | None = None
) -> bool:
    """Verify a capability descriptor asset meets the 040 trust gate (G10).

    Returns True if:
    1. Canonical seal valid (verify_asset_seal passes)
    2. Trust tier at or above minimum ("configured"), or passes *policy* evaluation
    3. Name prefix matches expected category (if *kind* provided; unknown kind rejected)
    4. Dual-hash integrity (definition_hash + content_hash non-empty)

    When *policy* is provided and has a minimum_trust_tier, it is used instead of
    the built-in ``_MINIMUM_TRUST_TIER`` gate.  When *policy* is None the existing
    default gate behaviour is preserved.

    Gate: G10 (Trust, Signatures, and Sealed Config Policy)
    """
    if not verify_asset_seal(descriptor):
        return False

    if policy is not None:
        result = policy.evaluate([descriptor])
        if not result.accepted:
            return False
    else:
        try:
            tier = TrustTier.from_str(descriptor.trust_tier)
        except ValueError:
            return False
        min_tier = TrustTier.from_str(_MINIMUM_TRUST_TIER)
        if tier.value < min_tier.value:
            return False

    if kind is not None:
        if kind not in CAPABILITY_KINDS:
            return False
        expected_prefix = _NAME_PREFIX[kind]
        if not descriptor.name.startswith(expected_prefix):
            return False

    if not descriptor.definition_hash or not descriptor.content_hash:
        return False

    return True
