"""Tests for capability descriptor asset creation."""

from __future__ import annotations

import json


from aigineering.core.capability_descriptors import (
    CAPABILITY_KINDS,
    create_tool_descriptor,
    create_mcp_descriptor,
    create_skill_descriptor,
    create_memory_descriptor,
    create_persona_descriptor,
    verify_descriptor,
)
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset


# ---------------------------------------------------------------------------
# test_tool_descriptor_registration_and_query
# ---------------------------------------------------------------------------


def test_tool_descriptor_registration_and_query():
    """A tool descriptor can be stored in a MemoryStore and queried back with
    all expected metadata fields intact."""
    store = MemoryStore()

    descriptor = create_tool_descriptor(
        name="web_search",
        description="Search the web for information.",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        trust_tier="verified",
        source_uri="tool://web_search/1.0",
    )

    store._add_system_asset(descriptor)

    # Query by exact ID
    loaded = store.get_asset(descriptor.id)
    assert loaded is not None
    assert loaded == descriptor

    # Query by name
    by_name = store.get_assets_by_name("_tool_capability_web_search")
    assert len(by_name) == 1
    assert by_name[0] == descriptor

    # Verify content fields
    content = json.loads(loaded.content)
    assert content["kind"] == "tool"
    assert content["name"] == "web_search"
    assert content["version"] == "0.1.0"
    assert content["description"] == "Search the web for information."
    assert content["input_schema"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
    }
    assert content["sealed_config_ref"] == ""

    # Verify provenance on the Asset object itself
    assert loaded.origin == "capability_registry"
    assert loaded.trust_tier == "verified"
    assert loaded.minted_by == "capability_registry"
    assert loaded.source_uri == "tool://web_search/1.0"


# ---------------------------------------------------------------------------
# test_mcp_descriptor_sealed_config
# ---------------------------------------------------------------------------


def test_mcp_descriptor_sealed_config():
    """An MCP descriptor must NOT contain private transport credentials or
    endpoint secrets in its disclosed content.  Only a sealed_config_ref
    reference is present."""
    descriptor = create_mcp_descriptor(
        name="github",
        source_uri="mcp://github/api",
        trust_tier="verified",
    )

    content = json.loads(descriptor.content)

    # Verify core fields
    assert content["kind"] == "mcp"
    assert content["name"] == "github"
    assert content["source_uri"] == "mcp://github/api"

    # sealed_config_ref is present but is a REFERENCE, not the config
    assert "sealed_config_ref" in content
    assert isinstance(content["sealed_config_ref"], str)

    # Private config MUST NOT be in disclosed content
    for key in (
        "api_key",
        "access_token",
        "secret",
        "password",
        "credential",
        "token",
        "private_key",
        "auth",
    ):
        assert key not in content, (
            f"Private config key '{key}' leaked into MCP descriptor content"
        )

    # The descriptor itself should have proper provenance
    assert descriptor.origin == "capability_registry"
    assert descriptor.name == "_mcp_github"
    assert verify_asset_seal(descriptor) is True


# ---------------------------------------------------------------------------
# test_all_five_capability_kinds
# ---------------------------------------------------------------------------


def test_all_five_capability_kinds():
    """All five CAPABILITY_KINDS produce valid, distinct descriptor Assets."""
    descriptors: dict[str, Asset] = {}

    descriptors["tool"] = create_tool_descriptor(
        name="lookup",
        description="Look up values.",
        input_schema={"type": "object"},
    )

    descriptors["mcp"] = create_mcp_descriptor(
        name="filesystem",
        source_uri="mcp://filesystem",
    )

    descriptors["skill"] = create_skill_descriptor(
        name="audit",
        content="Perform security audit.",
    )

    descriptors["memory"] = create_memory_descriptor(
        name="conversation",
        source_uri="memory://conversation",
    )

    descriptors["persona"] = create_persona_descriptor(
        name="helpful_assistant",
        content="You are a helpful assistant.",
    )

    # Every kind in CAPABILITY_KINDS must map to a descriptor
    assert set(descriptors.keys()) == set(CAPABILITY_KINDS)

    for kind, desc in descriptors.items():
        assert isinstance(desc, Asset)
        content = json.loads(desc.content)

        # Each descriptor declares its kind
        assert content["kind"] == kind

        # Each has a version
        assert "version" in content

        # Each has a sealed_config_ref (may be empty)
        assert "sealed_config_ref" in content

        # Each has provenance
        assert desc.origin == "capability_registry"
        assert desc.minted_by == "capability_registry"
        assert desc.signed_by
        assert desc.provenance_seal.startswith("asig_")

        # Names follow expected conventions
        if kind == "tool":
            assert desc.name == "_tool_capability_lookup"
        elif kind == "mcp":
            assert desc.name == "_mcp_filesystem"
        elif kind == "skill":
            assert desc.name == "_skill_capability_audit"
        elif kind == "memory":
            assert desc.name == "_memory_capability_conversation"
        elif kind == "persona":
            assert desc.name == "_persona_capability_helpful_assistant"

    # All IDs are distinct
    ids = [d.id for d in descriptors.values()]
    assert len(set(ids)) == 5


def test_default_capability_descriptors_pass_g10_gate():
    """Configured capability ingress should not create unusable descriptors."""
    descriptors = {
        "tool": create_tool_descriptor(
            name="lookup",
            description="Look up values.",
            input_schema={"type": "object"},
        ),
        "mcp": create_mcp_descriptor(
            name="filesystem",
            source_uri="mcp://filesystem",
        ),
        "skill": create_skill_descriptor(
            name="audit",
            content="Perform security audit.",
        ),
        "memory": create_memory_descriptor(
            name="conversation",
            source_uri="memory://conversation",
        ),
        "persona": create_persona_descriptor(
            name="helpful_assistant",
            content="You are helpful.",
        ),
    }

    for kind, descriptor in descriptors.items():
        assert descriptor.trust_tier == "configured"
        assert verify_descriptor(descriptor, kind=kind)


# ---------------------------------------------------------------------------
# test_descriptor_disclosure_metadata_only
# ---------------------------------------------------------------------------


def test_descriptor_disclosure_metadata_only():
    """Capability descriptor content is disclosure-safe metadata — no hidden
    configuration is embedded in the asset content."""
    # Create one descriptor of each kind with "sensitive-looking" source URIs
    t = create_tool_descriptor(
        name="secure_api",
        description="Call secured API endpoint.",
        input_schema={"type": "object"},
        trust_tier="verified",
        source_uri="tool://secure_api/v2",
    )

    m = create_mcp_descriptor(
        name="private_registry",
        source_uri="mcp+https://private.example.com",
        trust_tier="verified",
    )

    s = create_skill_descriptor(
        name="deploy",
        content="Deploy to production.",
        trust_tier="verified",
    )

    mem = create_memory_descriptor(
        name="enterprise_db",
        source_uri="memory://enterprise/read",
        trust_tier="verified",
    )

    p = create_persona_descriptor(
        name="sudo_user",
        content="You are a privileged user.",
        trust_tier="verified",
    )

    all_descriptors = [t, m, s, mem, p]

    for desc in all_descriptors:
        content = json.loads(desc.content)

        # Content is plain metadata — not hidden payloads
        assert isinstance(content, dict)
        assert "kind" in content
        assert "name" in content
        assert "version" in content
        assert "sealed_config_ref" in content

        # source_uri on the Asset object is disclosure-safe provenance
        assert isinstance(desc.source_uri, str)

        # No binary or encoded private data in content
        for key, val in content.items():
            if isinstance(val, str):
                assert not val.startswith("Bearer "), (
                    f"Auth token leaked in {desc.name}"
                )
                assert not val.startswith("Basic "), f"Auth token leaked in {desc.name}"

    # Verify the store round-trip does not add hidden fields
    store = MemoryStore()
    store._add_system_asset(t)
    rel = store.get_asset(t.id)
    assert rel is not None
    assert rel == t
    reloaded = json.loads(rel.content)
    assert reloaded == json.loads(t.content)


# ---------------------------------------------------------------------------
# test_descriptors_carry_provenance
# ---------------------------------------------------------------------------


def test_descriptors_carry_provenance():
    """Every capability descriptor carries full provenance metadata:
    origin, trust_tier, minted_by, source_uri, signed_by, signature
    — and the signature verifies."""
    descriptors = [
        create_tool_descriptor(
            name="reader",
            description="Read files.",
            input_schema={"type": "object"},
            trust_tier="observed",
            source_uri="tool://reader",
        ),
        create_mcp_descriptor(
            name="db",
            source_uri="mcp://db",
            trust_tier="verified",
        ),
        create_skill_descriptor(
            name="format",
            content="Format output.",
            trust_tier="verified",
        ),
        create_memory_descriptor(
            name="chat_history",
            source_uri="memory://chat",
            trust_tier="observed",
        ),
        create_persona_descriptor(
            name="reviewer",
            content="You are a code reviewer.",
            trust_tier="verified",
        ),
    ]

    expected_trust_tiers = ["observed", "verified", "verified", "observed", "verified"]

    for desc, expected_trust in zip(descriptors, expected_trust_tiers):
        # Core provenance fields are present
        assert desc.origin == "capability_registry"
        assert desc.trust_tier == expected_trust
        assert desc.minted_by == "capability_registry"

        # Signature fields are populated
        assert desc.signed_by
        assert desc.provenance_seal.startswith("asig_")

        # Signature verifies
        assert verify_asset_seal(desc) is True

        # ID and hash fields are computed
        assert desc.id.startswith("cap:")
        assert desc.definition_hash.startswith("def:")
        assert desc.content_hash.startswith("content:")

    # Tampered content breaks signature verification
    from dataclasses import replace

    tampered = replace(descriptors[0], content="tampered content")
    assert verify_asset_seal(tampered) is False

    # Provenance is preserved through store round-trip
    store = MemoryStore()
    for desc in descriptors:
        store._add_system_asset(desc)

    for desc in descriptors:
        reloaded = store.get_asset(desc.id)
        assert reloaded is not None
        assert reloaded.origin == "capability_registry"
        assert reloaded.minted_by == "capability_registry"
        assert reloaded.signed_by == desc.signed_by
        assert reloaded.provenance_seal == desc.provenance_seal
        assert verify_asset_seal(reloaded) is True
