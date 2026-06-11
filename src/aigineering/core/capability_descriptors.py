"""Capability descriptor assets for tools, MCPs, skills, memory, and personas.

Descriptors are Assets that carry origin, trust, and provenance metadata.
They describe what capabilities exist without exposing private configuration.
Private config (API keys, secrets) is sealed — only a reference hash is disclosed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aigineering.core.ids import compute_content_hash, hash_asset_content, hash_asset_definition
from aigineering.core.provenance import sign_asset
from aigineering.protocol.types import Asset

CAPABILITY_KINDS = ("tool", "mcp", "skill", "memory", "persona")

_NAME_PREFIX: dict[str, str] = {
    "tool": "_tool_capability_",
    "mcp": "_mcp_",
    "skill": "_skill_capability_",
    "memory": "_memory_capability_",
    "persona": "_persona_capability_",
}


def _build_descriptor_asset(
    kind: str,
    name: str,
    disclosed_content: dict[str, Any],
    trust_tier: str = "untrusted",
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
    trust_tier: str = "untrusted",
    source_uri: str = "",
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
        Trust tier for the tool (default ``"untrusted"``).
    source_uri : str
        URI identifying the tool's origin (e.g. ``"tool://web_search"``).

    Returns
    -------
    Asset
        Signed descriptor Asset named ``_tool_capability_{name}``.
    """
    disclosed = {
        "kind": "tool",
        "name": name,
        "version": "0.1.0",
        "description": description,
        "input_schema": dict(input_schema),
        "sealed_config_ref": "",
    }
    return _build_descriptor_asset("tool", name, disclosed, trust_tier, source_uri)


def create_mcp_descriptor(
    name: str,
    source_uri: str,
    trust_tier: str = "untrusted",
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
        Trust tier for the MCP (default ``"untrusted"``).
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
    trust_tier: str = "untrusted",
) -> Asset:
    """Create a capability descriptor Asset for a Skill / procedure.

    Parameters
    ----------
    name : str
        Skill name (e.g. ``"security_review"``).
    content : str
        The skill definition / procedure content.
    trust_tier : str
        Trust tier for the skill (default ``"untrusted"``).

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
        "content": content,
        "sealed_config_ref": "",
    }
    return _build_descriptor_asset("skill", name, disclosed, trust_tier)


def create_memory_descriptor(
    name: str,
    source_uri: str,
    trust_tier: str = "untrusted",
) -> Asset:
    """Create a capability descriptor Asset for a Memory / context provider.

    Parameters
    ----------
    name : str
        Memory capability name (e.g. ``"session_context"``).
    source_uri : str
        URI identifying the memory source (e.g. ``"memory://session"``).
    trust_tier : str
        Trust tier for the memory capability (default ``"untrusted"``).

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
    trust_tier: str = "untrusted",
) -> Asset:
    """Create a capability descriptor Asset for a Persona / policy.

    Parameters
    ----------
    name : str
        Persona name (e.g. ``"auditor"``).
    content : str
        The persona definition / policy content.
    trust_tier : str
        Trust tier for the persona (default ``"untrusted"``).

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
        "content": content,
        "sealed_config_ref": "",
    }
    return _build_descriptor_asset("persona", name, disclosed, trust_tier)
