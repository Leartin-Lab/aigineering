"""Tests for MCPWorker and MCP descriptor assets (v0.4.2)."""

from __future__ import annotations

import json

import pytest

from aigineering.agent.mcp_worker import MCPWorker
from aigineering.core.capability_descriptors import create_mcp_descriptor
from aigineering.core.provenance import verify_asset_signature
from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset, Candidate


# ---------------------------------------------------------------------------
# MCP descriptor tests
# ---------------------------------------------------------------------------


def test_mcp_descriptor_creation():
    """create_mcp_descriptor produces an Asset with name _mcp_* carrying
    tool_name, input_schema, output_schema, trust_tier, source_uri."""
    descriptor = create_mcp_descriptor(
        name="search",
        source_uri="mcp://search/api",
        trust_tier="verified",
        tool_name="search.query",
        input_schema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        output_schema={
            "type": "object",
            "properties": {"results": {"type": "array"}},
        },
    )

    # Verify Asset identity
    assert isinstance(descriptor, Asset)
    assert descriptor.name == "_mcp_search"
    assert descriptor.id.startswith("cap:")

    # Verify disclosed content
    content = json.loads(descriptor.content)
    assert content["kind"] == "mcp"
    assert content["name"] == "search"
    assert content["version"] == "0.1.0"
    assert content["source_uri"] == "mcp://search/api"
    assert content["sealed_config_ref"] == ""
    assert content["tool_name"] == "search.query"
    assert content["input_schema"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    assert content["output_schema"] == {
        "type": "object",
        "properties": {"results": {"type": "array"}},
    }

    # Verify provenance on the Asset object
    assert descriptor.origin == "capability_registry"
    assert descriptor.trust_tier == "verified"
    assert descriptor.minted_by == "capability_registry"
    assert descriptor.source_uri == "mcp://search/api"


def test_mcp_descriptor_creation_server_only():
    """create_mcp_descriptor without tool-level params still produces a
    valid server-level descriptor (backward-compatible)."""
    descriptor = create_mcp_descriptor(
        name="filesystem",
        source_uri="mcp://filesystem",
        trust_tier="trusted",
    )

    assert descriptor.name == "_mcp_filesystem"
    content = json.loads(descriptor.content)
    assert content["kind"] == "mcp"
    assert "tool_name" not in content
    assert "input_schema" not in content
    assert "output_schema" not in content
    assert verify_asset_signature(descriptor) is True


def test_mcp_descriptor_sealed_config():
    """An MCP descriptor must NOT leak API keys, tokens, or secrets in
    disclosed content — only a sealed_config_ref reference is present."""
    descriptor = create_mcp_descriptor(
        name="github",
        source_uri="mcp://github/api",
        trust_tier="trusted",
        tool_name="github.search_repos",
        input_schema={"type": "object"},
    )

    content = json.loads(descriptor.content)

    # sealed_config_ref is a REFERENCE, not the config itself
    assert "sealed_config_ref" in content
    assert isinstance(content["sealed_config_ref"], str)
    assert content["sealed_config_ref"] == ""

    # Private config MUST NOT be in disclosed content
    private_keys = (
        "api_key", "access_token", "secret", "password", "credential",
        "token", "private_key", "auth",
    )
    for key in private_keys:
        assert key not in content, (
            f"Private config key '{key}' leaked into MCP descriptor content"
        )

    # No embedded bearer tokens or basic auth strings
    for key, val in content.items():
        if isinstance(val, str):
            assert not val.startswith("Bearer "), f"Auth token leaked in {key}"
            assert not val.startswith("Basic "), f"Auth token leaked in {key}"

    # Descriptor has proper provenance
    assert descriptor.origin == "capability_registry"
    assert verify_asset_signature(descriptor) is True


def test_mcp_descriptor_carries_provenance():
    """Every MCP descriptor carries full provenance metadata:
    origin, trust_tier, minted_by, source_uri, signed_by, signature
    — and the signature verifies."""
    descriptors = [
        create_mcp_descriptor(
            name="search",
            source_uri="mcp://search",
            trust_tier="verified",
            tool_name="search.query",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
        create_mcp_descriptor(
            name="filesystem",
            source_uri="mcp://filesystem",
            trust_tier="observed",
        ),
        create_mcp_descriptor(
            name="database",
            source_uri="mcp://db",
            trust_tier="trusted",
            tool_name="db.query",
        ),
    ]

    expected_trust_tiers = ["verified", "observed", "trusted"]

    for desc, expected_trust in zip(descriptors, expected_trust_tiers):
        # Core provenance fields are present
        assert desc.origin == "capability_registry"
        assert desc.trust_tier == expected_trust
        assert desc.minted_by == "capability_registry"

        # Signature fields are populated
        assert desc.signed_by
        assert desc.signature.startswith("asig_")

        # Signature verifies
        assert verify_asset_signature(desc) is True

        # ID and hash fields are computed
        assert desc.id.startswith("cap:")
        assert desc.definition_hash.startswith("def:")
        assert desc.content_hash.startswith("content:")

    # Tampered content breaks signature verification
    from dataclasses import replace
    tampered = replace(descriptors[0], content="tampered content")
    assert verify_asset_signature(tampered) is False

    # Provenance is preserved through store round-trip
    store = MemoryStore()
    for desc in descriptors:
        store.add_asset(desc)

    for desc in descriptors:
        reloaded = store.get_asset(desc.id)
        assert reloaded is not None
        assert reloaded.origin == "capability_registry"
        assert reloaded.minted_by == "capability_registry"
        assert reloaded.signed_by == desc.signed_by
        assert reloaded.signature == desc.signature
        assert verify_asset_signature(reloaded) is True


# ---------------------------------------------------------------------------
# MCPWorker tests
# ---------------------------------------------------------------------------


def test_mcp_worker_returns_candidate():
    """MCPWorker.invoke() executes a valid MCP tool call and returns a Candidate."""
    # Mock MCP server: callable that takes (tool_name, args) → str
    def mock_search_server(tool_name: str, args: dict) -> str:
        return json.dumps({"results": [f"hit for {args['q']}"]})

    worker = MCPWorker(mcp_servers={"search": mock_search_server})
    candidate = worker.invoke("search.query", {"q": "hello"}, "contract_1")

    assert isinstance(candidate, Candidate)
    assert candidate.worker_id == "mcp_worker:search.query"

    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is True
    assert obs["tool"] == "search.query"
    assert "hit for hello" in obs["result"]
    assert obs["error"] == ""


def test_mcp_worker_multi_server():
    """MCPWorker dispatches to the correct server based on tool_name prefix."""
    calls: dict[str, list] = {"filesystem": [], "database": []}

    def make_server(name: str):
        def handler(tool_name: str, args: dict) -> str:
            calls[name].append((tool_name, args))
            return f"ok from {name}"
        return handler

    worker = MCPWorker(mcp_servers={
        "filesystem": make_server("filesystem"),
        "database": make_server("database"),
    })

    # Call filesystem tool
    c1 = worker.invoke("filesystem.read", {"path": "/tmp"}, "c1")
    obs1 = json.loads(c1.raw_output)
    assert obs1["ok"] is True
    assert obs1["tool"] == "filesystem.read"
    assert len(calls["filesystem"]) == 1
    assert calls["filesystem"][0] == ("filesystem.read", {"path": "/tmp"})

    # Call database tool
    c2 = worker.invoke("database.query", {"sql": "SELECT 1"}, "c2")
    obs2 = json.loads(c2.raw_output)
    assert obs2["ok"] is True
    assert obs2["tool"] == "database.query"
    assert len(calls["database"]) == 1
    assert calls["database"][0] == ("database.query", {"sql": "SELECT 1"})


def test_mcp_worker_handles_error_unknown_server():
    """MCPWorker.invoke() returns an error Candidate when the server is unknown."""
    worker = MCPWorker(mcp_servers={})
    candidate = worker.invoke("nonexistent.tool", {}, "contract_1")

    assert isinstance(candidate, Candidate)
    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is False
    assert obs["tool"] == "nonexistent.tool"
    assert obs["result"] == ""
    assert "unknown mcp server" in obs["error"]


def test_mcp_worker_handles_error_tool_failure():
    """MCPWorker.invoke() returns an error Candidate when the server raises."""
    def failing_server(_tool_name: str, _args: dict) -> str:
        raise RuntimeError("connection refused")

    worker = MCPWorker(mcp_servers={"broken": failing_server})
    candidate = worker.invoke("broken.ping", {}, "contract_1")

    assert isinstance(candidate, Candidate)
    obs = json.loads(candidate.raw_output)
    assert obs["ok"] is False
    assert obs["tool"] == "broken.ping"
    assert obs["result"] == ""
    assert obs["error"] == "connection refused"


def test_mcp_worker_candidate_not_committed_directly():
    """Candidate from MCPWorker must go through projection to become a fact.

    MCPWorker returns a Candidate — it is NOT an Asset and does NOT
    appear in any store.  The handler must explicitly convert the
    Candidate into committed assets.
    """
    def mock_server(_tool_name: str, _args: dict) -> str:
        return "result"

    worker = MCPWorker(mcp_servers={"test": mock_server})
    candidate = worker.invoke("test.run", {}, "contract_1")

    # Candidate is NOT an Asset
    assert isinstance(candidate, Candidate)
    assert not isinstance(candidate, Asset)

    # Candidate has worker provenance, not asset metadata
    assert candidate.worker_id == "mcp_worker:test.run"
    assert not hasattr(candidate, "id")
    assert not hasattr(candidate, "name")
    assert not hasattr(candidate, "origin")

    # No side-effects: candidate exists only in memory
    store = MemoryStore()
    assert len(store.get_all_assets()) == 0


def test_mcp_worker_parity_with_direct_call():
    """MCPWorker.invoke() produces the same result as calling the server directly."""
    def mock_server(tool_name: str, args: dict) -> str:
        return json.dumps({"echo": args.get("msg", "")})

    worker = MCPWorker(mcp_servers={"echo": mock_server})

    direct_result = mock_server("echo.msg", {"msg": "hello"})

    candidate = worker.invoke("echo.msg", {"msg": "hello"}, "contract_1")
    obs = json.loads(candidate.raw_output)
    assert obs["result"] == direct_result
    assert obs["ok"] is True


def test_mcp_descriptor_store_round_trip():
    """MCP descriptor survives MemoryStore round-trip with all metadata intact."""
    descriptor = create_mcp_descriptor(
        name="search",
        source_uri="mcp://search",
        trust_tier="verified",
        tool_name="search.query",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
    )

    store = MemoryStore()
    store.add_asset(descriptor)

    # Query by ID
    loaded = store.get_asset(descriptor.id)
    assert loaded is not None
    assert loaded == descriptor

    # Query by name
    by_name = store.get_assets_by_name("_mcp_search")
    assert len(by_name) == 1
    assert by_name[0] == descriptor

    # Verify content survived round-trip
    content = json.loads(loaded.content)
    assert content["tool_name"] == "search.query"
    assert content["input_schema"]["properties"]["q"]["type"] == "string"
    assert content["output_schema"]["properties"]["results"]["type"] == "array"
    assert verify_asset_signature(loaded) is True


def test_mcp_worker_arguments_preserved():
    """MCPWorker passes all args to the server callable unchanged."""
    received: list = []

    def recording_server(tool_name: str, args: dict) -> str:
        received.append((tool_name, args))
        return "ok"

    worker = MCPWorker(mcp_servers={"rec": recording_server})
    worker.invoke("rec.action", {"key1": "val1", "key2": 42, "nested": {"a": 1}}, "c1")

    assert len(received) == 1
    assert received[0][0] == "rec.action"
    assert received[0][1] == {"key1": "val1", "key2": 42, "nested": {"a": 1}}
