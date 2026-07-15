"""Tests for control-plane asset injection."""

import json

import pytest

from aigineering.core.authority import RESERVED_PREFIXES
from aigineering.core.control_plane import (
    inject_asset,
    inject_contract,
    _is_protected_name,
)
from aigineering.core.ids import hash_asset_definition, hash_asset_content
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore


def _make_ingress(store, trace):
    return RuntimeIngress(store, trace)


class TestInjectAsset:
    def test_inject_basic_asset(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        asset = inject_asset(
            store, trace, name="data_file", content="hello world", ingress=ingress
        )

        assert asset.name == "data_file"
        assert asset.content == "hello world"
        assert asset.definition_hash is not None
        assert asset.content_hash is not None
        assert asset.signed_by is not None
        assert asset.provenance_seal is not None
        assert asset.origin == "human"
        assert asset.trust_tier == "human"

        # Verify persistence
        loaded = store.get_asset(asset.id)
        assert loaded is not None
        assert loaded.content == "hello world"

        # Verify trace (ingress uses "asset_accepted" event type)
        events = trace.get_by_event_type("asset_accepted")
        assert len(events) >= 1

    def test_inject_with_custom_origin_and_tier(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        asset = inject_asset(
            store,
            trace,
            name="config",
            content="{}",
            origin="imported",
            trust_tier="configured",
            source_uri="file://config.json",
            promptable=False,
            content_type="application/json",
            ingress=ingress,
        )

        assert asset.origin == "imported"
        assert asset.trust_tier == "configured"
        assert asset.source_uri == "file://config.json"
        assert asset.promptable is False
        assert asset.content_type == "application/json"

    def test_protected_name_rejected_by_default(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        for name in (
            "_sys_secret",
            "_tool_obs_lookup",
            "_mcp_filesystem",
            "_skill_review",
        ):
            with pytest.raises(ValueError, match="protected prefix"):
                inject_asset(store, trace, name=name, content="test", ingress=ingress)

    def test_protected_name_allowed_with_override(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        asset = inject_asset(
            store,
            trace,
            name="_sys_admin_config",
            content="admin",
            allow_protected=True,
            ingress=ingress,
        )

        assert asset.name == "_sys_admin_config"
        loaded = store.get_asset(asset.id)
        assert loaded is not None

        # Verify distinct event types for normal acceptance and override
        accepted = trace.get_by_event_type("asset_accepted")
        overrides = trace.get_by_event_type("asset_accepted_protected_override")
        assert len(accepted) >= 1
        assert len(overrides) >= 1

    def test_hashes_are_deterministic(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        a1 = inject_asset(store, trace, name="foo", content="bar", ingress=ingress)
        a2 = inject_asset(store, trace, name="foo", content="bar", ingress=ingress)

        assert a1.id == a2.id
        assert a1.definition_hash == a2.definition_hash
        assert a1.content_hash == a2.content_hash

    def test_inject_preserves_content_hash_integrity(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        asset = inject_asset(
            store, trace, name="doc", content="important data", ingress=ingress
        )

        expected_hash = hash_asset_content("doc", "important data")
        assert asset.content_hash == expected_hash
        assert asset.definition_hash == hash_asset_definition("doc")

    def test_trace_contains_audit_data(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        inject_asset(
            store,
            trace,
            name="audit_test",
            content="data",
            origin="imported",
            trust_tier="configured",
            ingress=ingress,
        )

        events = trace.get_by_event_type("asset_accepted")
        assert len(events) >= 1
        entry = events[0]

        # Verify audit data is in accepted_fragments
        assert len(entry.accepted_fragments) >= 1
        audit = json.loads(entry.accepted_fragments[0])
        assert "asset_id" in audit
        assert audit.get("origin") == "imported" or audit.get("origin") is None
        assert (
            audit.get("trust_tier") == "configured" or audit.get("trust_tier") is None
        )

    def test_trace_ids_are_unique(self):
        """Each trace entry must have a unique ID (no collisions from duplicate sequence)."""
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        inject_asset(store, trace, name="alpha", content="a", ingress=ingress)
        inject_asset(store, trace, name="beta", content="b", ingress=ingress)
        inject_asset(
            store,
            trace,
            name="_sys_override",
            content="c",
            allow_protected=True,
            ingress=ingress,
        )

        all_events = trace.get_all()
        ids = [e.id for e in all_events]
        assert len(ids) == len(set(ids)), f"Duplicate trace IDs found: {ids}"

        # Verify the override produced a distinct event type
        override_events = trace.get_by_event_type("asset_accepted_protected_override")
        assert len(override_events) >= 1

    def test_protected_set_equals_authority_reserved_prefixes(self):
        """control_plane._is_protected_name uses the same RESERVED_PREFIXES
        as authority.py (single source of truth)."""
        # Every reserved prefix from authority should be caught by _is_protected_name
        for prefix in sorted(RESERVED_PREFIXES):
            assert _is_protected_name(prefix + "example"), (
                f"RESERVED_PREFIX '{prefix}' should be protected"
            )

        # Non-reserved names should not be rejected
        assert not _is_protected_name("normal_asset")
        assert not _is_protected_name("my_data_file")


class TestProtectedPrefixes:
    def test_is_protected_prefix_matches(self):
        assert _is_protected_name("_sys_config") is True
        assert _is_protected_name("_mcp_filesystem") is True
        assert _is_protected_name("_tool_obs_result") is True
        assert _is_protected_name("_skill_review") is True
        assert _is_protected_name("normal_asset") is False

    def test_is_protected_edge_cases(self):
        assert _is_protected_name("_sys") is True  # exact match
        assert _is_protected_name("sys_config") is False  # no underscore
        assert _is_protected_name("") is False

    def test_previously_missing_prefixes_now_protected(self):
        """Prefixes from authority.py that were not in the old
        _CONTROL_PLANE_PROTECTED_PREFIXES must now be caught."""
        assert _is_protected_name("_method_ctx_some_value") is True
        assert _is_protected_name("_memory_value") is True
        assert _is_protected_name("_retry_something") is True
        assert _is_protected_name("_soul_data") is True
        assert _is_protected_name("_fail_result_output") is True
        assert _is_protected_name("_replan_report_output") is True
        assert _is_protected_name("_fail_report_output") is True
        assert _is_protected_name("_persona_config") is True

    def test_protected_prefixes_rejected_in_injection(self):
        """Prefixes from authority.py that were previously missing must
        now cause ValueError on inject_asset."""
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)

        newly_protected = [
            "_method_ctx_test",
            "_memory_test",
            "_retry_test",
            "_soul_test",
            "_fail_result_test",
            "_replan_report_test",
            "_fail_report_test",
            "_persona_test",
        ]
        for name in newly_protected:
            with pytest.raises(ValueError, match="protected prefix"):
                inject_asset(store, trace, name=name, content="test", ingress=ingress)


class TestInjectContract:
    def test_inject_basic_contract(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)
        contract = inject_contract(
            store,
            trace,
            name="build_report",
            inputs=("data_file",),
            outputs=("final_report",),
            activation="data_file",
            budget=5,
            ingress=ingress,
        )
        assert contract.name == "build_report"
        assert contract.id.startswith("task:")
        assert contract.budget == 5
        loaded = store.get_contract(contract.id)
        assert loaded is not None

    def test_protected_output_rejected_by_default(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)
        for output in ("_sys_config", "_mcp_filesystem", "_skill_review"):
            with pytest.raises(ValueError, match="protected"):
                inject_contract(
                    store, trace, name="bad", outputs=(output,), ingress=ingress
                )

    def test_protected_output_override_cannot_grant_runtime_authority(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)
        with pytest.raises(
            ValueError, match="cannot receive runtime minting authority"
        ):
            inject_contract(
                store,
                trace,
                name="admin",
                outputs=("_sys_config",),
                allow_protected_outputs=True,
                ingress=ingress,
            )

    def test_trace_recorded(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)
        inject_contract(store, trace, name="traced", outputs=("out",), ingress=ingress)
        events = trace.get_by_event_type("contract_accepted")
        assert len(events) >= 1

    def test_deterministic_id(self):
        store = MemoryStore()
        trace = MemoryTraceStore()
        ingress = _make_ingress(store, trace)
        c1 = inject_contract(
            store, trace, name="foo", outputs=("x",), budget=3, ingress=ingress
        )
        c2 = inject_contract(
            store, trace, name="foo", outputs=("x",), budget=3, ingress=ingress
        )
        assert c1.id == c2.id
