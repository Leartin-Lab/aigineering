"""Tests for ReservedNamespaceError and reserved namespace protection."""

import pytest

from aigineering.core.authority import RESERVED_PREFIXES, ReservedNamespaceError
from aigineering.core.ids import hash_asset_content
from aigineering.core.provenance import sign_asset
from conftest import candidate_runtime
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset


def test_reserved_namespace_error_is_defined():
    """ReservedNamespaceError exists and is an Exception subclass."""
    assert issubclass(ReservedNamespaceError, Exception)

    err = ReservedNamespaceError("_sys_config", "_sys_")
    assert err.name == "_sys_config"
    assert err.prefix == "_sys_"
    assert "collides with reserved prefix" in str(err)


def test_reserved_namespace_error_with_each_prefix():
    """ReservedNamespaceError works with every reserved prefix."""
    for prefix in RESERVED_PREFIXES:
        name = f"{prefix}test_asset"
        err = ReservedNamespaceError(name, prefix)
        assert err.name == name
        assert err.prefix == prefix
        assert name in str(err)
        assert prefix in str(err)


def test_reserved_namespace_error_is_raisable():
    """ReservedNamespaceError can be raised and caught as Exception."""
    with pytest.raises(ReservedNamespaceError) as exc_info:
        raise ReservedNamespaceError("_memory_data", "_memory_")

    assert exc_info.value.name == "_memory_data"
    assert exc_info.value.prefix == "_memory_"


def test_reserved_prefixes_are_not_empty():
    """RESERVED_PREFIXES contains the expected runtime-only namespaces."""
    assert len(RESERVED_PREFIXES) >= 4
    assert "_sys_" in RESERVED_PREFIXES
    assert "_tool_obs_" in RESERVED_PREFIXES
    assert "_memory_" in RESERVED_PREFIXES
    assert "_persona_" in RESERVED_PREFIXES


def test_runtime_generated_capability_and_control_namespaces_are_reserved():
    assert {
        "_context_overflow_report_",
        "_file_content_",
        "_label_missing_",
        "_provider_config_",
        "_sufficiency_result_",
        "_tool_capability_",
    } <= RESERVED_PREFIXES


@pytest.mark.parametrize(
    "store_factory", [MemoryStore, lambda: SQLiteStore(":memory:")]
)
def test_public_store_write_rejects_protected_assets(store_factory):
    """The public persistence API cannot bypass the protected namespace gate."""
    store = store_factory()
    asset = sign_asset(
        Asset(
            id=hash_asset_content("_sys_test", "blocked"),
            name="_sys_test",
            content="blocked",
        ),
        signed_by="test",
    )
    with pytest.raises(ReservedNamespaceError):
        store.add_asset(asset)


def test_ingress_can_commit_authorized_protected_asset():
    """The ingress remains the explicit, traced privileged write path."""
    store = SQLiteStore(":memory:")
    ingress = candidate_runtime(store)
    asset = Asset(
        id=hash_asset_content("_sys_test", "allowed"),
        name="_sys_test",
        content="allowed",
    )
    accepted = ingress.accept_asset(asset, source="test", allow_protected=True)
    assert store.get_asset(accepted.id) is not None
    assert store.get_by_event_type("candidate_committed")
