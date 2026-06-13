"""Tests for provider config snapshot assets."""
from __future__ import annotations

import json

import pytest

from aigineering.core.provider_config import create_provider_config_snapshot
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset


# ---------------------------------------------------------------------------
# test_config_snapshot_created
# ---------------------------------------------------------------------------


def test_config_snapshot_created():
    """A provider config snapshot is created with all expected fields."""
    snapshot = create_provider_config_snapshot(
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        timeout=60.0,
        max_retries=3,
        capabilities=("json_schema", "tool_calling", "streaming"),
        trust_tier="configured",
    )

    assert isinstance(snapshot, Asset)
    assert snapshot.name == "_provider_config_openai"

    content = json.loads(snapshot.content)
    assert content["provider_name"] == "openai"
    assert content["base_url"] == "https://api.openai.com/v1"
    assert content["model"] == "gpt-4.1-mini"
    assert content["timeout"] == 60.0
    assert content["max_retries"] == 3
    assert content["capabilities"] == ["json_schema", "streaming", "tool_calling"]
    assert content["sealed_config_ref"] == ""


# ---------------------------------------------------------------------------
# test_api_key_not_in_content
# ---------------------------------------------------------------------------


def test_api_key_not_in_content():
    """The snapshot content MUST NOT contain any API key or secret."""
    snapshot = create_provider_config_snapshot(
        provider_name="vllm_local",
        base_url="http://localhost:8000/v1",
        model="meta-llama/Llama-3-8B-Instruct",
        timeout=120.0,
        max_retries=2,
        capabilities=("streaming",),
    )

    # The Asset content is plain JSON — deserialize and inspect
    content = json.loads(snapshot.content)

    for secret_key in (
        "api_key",
        "access_token",
        "secret",
        "password",
        "credential",
        "token",
        "private_key",
        "auth",
        "key",
        "bearer",
    ):
        assert secret_key not in content, (
            f"Secret key '{secret_key}' leaked into provider config content"
        )

    # Also verify no Bearer/Basic token strings in any value
    for val in content.values():
        if isinstance(val, str):
            assert not val.startswith("Bearer "), (
                f"Bearer token leaked in provider config"
            )
            assert not val.startswith("Basic "), (
                f"Basic auth token leaked in provider config"
            )

    # The sealed_config_ref is present but empty (no key leaked)
    assert "sealed_config_ref" in content
    assert content["sealed_config_ref"] == ""


# ---------------------------------------------------------------------------
# test_capabilities_listed
# ---------------------------------------------------------------------------


def test_capabilities_listed():
    """Provider capabilities are present and sorted in the snapshot content."""
    snapshot = create_provider_config_snapshot(
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4",
        timeout=30.0,
        max_retries=5,
        capabilities=("tool_calling", "json_schema", "streaming"),
    )

    content = json.loads(snapshot.content)

    caps = content["capabilities"]
    assert isinstance(caps, list)
    assert len(caps) == 3

    # Capabilities are sorted alphabetically
    assert caps == sorted(caps)

    # All expected capabilities are present
    assert "json_schema" in caps
    assert "streaming" in caps
    assert "tool_calling" in caps


def test_capabilities_empty_tuple():
    """An empty capabilities tuple is handled gracefully."""
    snapshot = create_provider_config_snapshot(
        provider_name="minimal",
        base_url="http://localhost:8000/v1",
        model="test-model",
        timeout=10.0,
        max_retries=1,
        capabilities=(),
    )

    content = json.loads(snapshot.content)
    assert content["capabilities"] == []


# ---------------------------------------------------------------------------
# test_provenance_metadata
# ---------------------------------------------------------------------------


def test_provenance_metadata():
    """The snapshot carries full provenance: origin, trust_tier, minted_by,
    source_uri, signed_by, signature — and the signature verifies."""
    snapshot = create_provider_config_snapshot(
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        timeout=60.0,
        max_retries=3,
        capabilities=("json_schema", "tool_calling"),
        trust_tier="verified",
    )

    # Core provenance fields
    assert snapshot.origin == "capability_registry"
    assert snapshot.trust_tier == "verified"
    assert snapshot.minted_by == "capability_registry"
    assert snapshot.source_uri == "provider://openai"

    # Signature fields are populated
    assert snapshot.signed_by
    assert snapshot.provenance_seal.startswith("asig_")

    # Signature verifies
    assert verify_asset_seal(snapshot) is True

    # ID and hash fields are computed
    assert snapshot.id.startswith("cap:")
    assert snapshot.definition_hash.startswith("def:")
    assert snapshot.content_hash.startswith("content:")


def test_provenance_default_trust_tier():
    """Default trust_tier is 'configured' when not explicitly set."""
    snapshot = create_provider_config_snapshot(
        provider_name="test_provider",
        base_url="http://localhost:8000/v1",
        model="test-model",
        timeout=10.0,
        max_retries=1,
        capabilities=(),
    )

    assert snapshot.trust_tier == "configured"


# ---------------------------------------------------------------------------
# test_snapshot_is_immutable
# ---------------------------------------------------------------------------


def test_snapshot_is_immutable():
    """The snapshot Asset uses frozen dataclass fields and rejects mutation."""
    snapshot = create_provider_config_snapshot(
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        timeout=60.0,
        max_retries=3,
        capabilities=("json_schema", "tool_calling"),
    )

    # Asset is a frozen dataclass — attribute assignment is rejected
    with pytest.raises((AttributeError, TypeError)):
        snapshot.name = "hijacked_name"  # type: ignore[misc]

    with pytest.raises((AttributeError, TypeError)):
        snapshot.content = "tampered"  # type: ignore[misc]

    with pytest.raises((AttributeError, TypeError)):
        snapshot.trust_tier = "untrusted"  # type: ignore[misc]

    # Content hash is stable (idempotent creation)
    snapshot2 = create_provider_config_snapshot(
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        timeout=60.0,
        max_retries=3,
        capabilities=("json_schema", "tool_calling"),
    )
    assert snapshot.id == snapshot2.id
    assert snapshot.content_hash == snapshot2.content_hash
    assert snapshot.provenance_seal == snapshot2.provenance_seal


# ---------------------------------------------------------------------------
# test_snapshot_store_round_trip
# ---------------------------------------------------------------------------


def test_snapshot_store_round_trip():
    """A provider config snapshot survives a MemoryStore round-trip intact."""
    store = MemoryStore()

    snapshot = create_provider_config_snapshot(
        provider_name="vllm_local",
        base_url="http://localhost:8000/v1",
        model="meta-llama/Llama-3-8B-Instruct",
        timeout=120.0,
        max_retries=2,
        capabilities=("streaming", "tool_calling"),
        trust_tier="configured",
    )

    store.add_asset(snapshot)

    # Query by exact ID
    loaded = store.get_asset(snapshot.id)
    assert loaded is not None
    assert loaded == snapshot

    # Query by name
    by_name = store.get_assets_by_name("_provider_config_vllm_local")
    assert len(by_name) == 1
    assert by_name[0] == snapshot

    # Verify content is identical after round-trip
    assert json.loads(loaded.content) == json.loads(snapshot.content)

    # Provenance is intact
    assert loaded.origin == "capability_registry"
    assert loaded.minted_by == "capability_registry"
    assert verify_asset_seal(loaded) is True
