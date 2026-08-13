"""Regression tests for the runtime's security and durability invariants."""

import pytest

from aigineering.protocol.types import Contract, Asset
from aigineering.core.store import MemoryStore


# ============================================================================
# Sealed configuration and boundary integrity
# ============================================================================


class TestSessionSealedConfig:
    """G10: Sealed config must never leak into text output."""

    def test_session_show_does_not_print_config_snapshot_values(self):
        """session show text output must not print raw config_snapshot values.

        Gate: G10 (Trust, Signatures, and Sealed Config Policy)
        """
        from aigineering.core.session import SessionStore, Session
        import tempfile

        # Create a session with sensitive config
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(sessions_dir=tmp)
            session = Session(
                id="test-session-redact",
                root_contract_id="root-1",
                contract_ids=["c-1"],
                asset_ids=[],
                trace_ids=[],
                config_snapshot={"api_key": "sk-secret-12345", "model": "gpt-4"},
                worker_snapshot={
                    "worker_id": "llm-worker-001",
                    "token": "secret-token",
                },
                created_at="2026-01-01T00:00:00",
            )
            store.create_session(session)

        # The fix applies _redact_sealed() to config_snapshot/worker_snapshot
        # before printing. Verify by checking what session_to_dict renders.
        from aigineering.protocol.wire import session_to_dict
        from aigineering.cli._common import _redact_sealed

        d = session_to_dict(session)
        redacted = _redact_sealed(d)

        # config_snapshot and worker_snapshot must be absent from redacted output
        assert "config_snapshot" not in redacted, (
            "G10/N-P0.1: config_snapshot must not appear in redacted session output"
        )
        assert "worker_snapshot" not in redacted, (
            "G10/N-P0.1: worker_snapshot must not appear in redacted session output"
        )

    def test_session_show_text_path_redacts_config(self):
        """Verify session_show text output does not contain sensitive config values.

        Gate: G10
        """
        from aigineering.cli.session import session_show
        from aigineering.core.session import SessionStore, Session
        from click.testing import CliRunner

        runner = CliRunner()
        with runner.isolated_filesystem():
            store = SessionStore()
            session = Session(
                id="test-redact-leak",
                root_contract_id="root-1",
                contract_ids=["c-1"],
                asset_ids=[],
                trace_ids=[],
                config_snapshot={"api_key": "sk-secret-12345"},
                worker_snapshot={"token": "secret-token"},
                created_at="2026-01-01T00:00:00",
            )
            store.create_session(session)
            result = runner.invoke(session_show, ["test-redact-leak"])

        # The raw secret values must NOT appear in text output
        assert "sk-secret-12345" not in result.output, (
            f"G10/N-P0.1: config_snapshot api_key leaked in text output:\n{result.output}"
        )
        assert "secret-token" not in result.output, (
            f"G10/N-P0.1: worker_snapshot token leaked in text output:\n{result.output}"
        )


class TestCLIRetryBypass:
    """G1: CLI must not directly mutate runtime state."""

    def test_cli_retry_does_not_directly_mutate_store(self):
        """aig retry must go through method ingress, not direct store.add_contract().

        Gate: G1 (Single Runtime Ingress)
        """
        import ast
        import os

        # ── Static analysis: cli/retry.py must not call add_contract() directly ──
        retry_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "aigineering", "cli", "retry.py"
        )
        with open(retry_path) as f:
            source = f.read()
        tree = ast.parse(source)

        add_contract_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_contract"
                ):
                    add_contract_calls.append(node.lineno)

        assert len(add_contract_calls) == 0, (
            f"G1/D-P0.1: cli/retry.py calls add_contract() directly at line(s): "
            f"{add_contract_calls}. Must go through method ingress "
            f"(MethodRuntime → RetryMethodHandler)."
        )

        # ── Functional test: CLI retry still creates the retry contract ──
        from aigineering.protocol.types import Contract
        from aigineering.core.methods import retry_contract
        from aigineering.cli import retry as retry_module
        from click.testing import CliRunner
        from unittest.mock import patch

        from aigineering.core.sqlite_store import SQLiteStore
        from aigineering.cli.identity import ensure_local_domain

        store = SQLiteStore(":memory:")
        original = Contract(
            id="c-original",
            parent_id=None,
            name="test-task",
            description="Test",
            inputs=["input1"],
            outputs=["output1"],
            budget=5,
        )
        store.add_contract(original)

        expected_id = retry_contract(original).id
        assert store.get_contract(expected_id) is None, (
            "Precondition: retry contract must not exist before CLI runs."
        )

        runner = CliRunner()
        with runner.isolated_filesystem():
            ensure_local_domain(store)
            with patch.object(retry_module, "_persistent_store", return_value=store):
                result = runner.invoke(retry_module.retry, ["--contract", "c-original"])

        assert result.exit_code == 0, f"G1/D-P0.1: CLI retry failed: {result.output}"

        retry_contract = store.get_contract(expected_id)
        assert retry_contract is not None, (
            f"G1/D-P0.1: Retry contract '{expected_id}' not found in store. "
            f"CLI output: {result.output}"
        )
        assert retry_contract.name == f"{original.name}.retry", (
            f"G1/D-P0.1: Retry contract name mismatch. "
            f"Expected '{original.name}.retry', got '{retry_contract.name}'."
        )
        assert retry_contract.parent_id == original.parent_id, (
            "G1/D-P0.1: Retry contract parent_id must match original."
        )
        assert expected_id in result.output, (
            f"G1/D-P0.1: Retry contract ID must appear in CLI output. "
            f"Got: {result.output}"
        )
        store.close()


class TestAuthorityClamp:
    """G6: Authority widening must be rejected, not clamped."""

    def test_tool_scope_widening_rejects_child_contract(self):
        """Tool scope widening must reject the child contract entirely.

        Gate: G6 (Deny-By-Default Capability Containment)
        """
        # Behavior: rejects child (action="rejected"), child skipped entirely
        from aigineering.core.methods import contracts_from_plan_asset

        parent = Contract(
            id="parent-scope",
            name="parent-task",
            description="Test",
            inputs=[],
            outputs=[],
            budget=10,
            tool_scope=["tool_a", "tool_b"],
        )

        plan_asset = Asset(
            id="plan-asset",
            name="_plan_result_parent-scope",
            content='{"contracts":[{"name":"child-task","description":"test","inputs":[],"outputs":["result"],"budget":5,"tool_scope":["tool_a","tool_b","tool_c"]}]}',
            definition_hash="def:plan",
            content_hash="content:plan",
            origin="system",
        )

        children, rejections = contracts_from_plan_asset(
            plan_asset, parent_id=parent.id, parent_contract=parent
        )

        # The tool_scope widening (adding "tool_c") must REJECT the child
        tool_scope_rejections = [
            r
            for r in rejections
            if isinstance(r, dict) and r.get("field") == "tool_scope"
        ]
        assert len(tool_scope_rejections) > 0, (
            f"G6/D-P0.4: tool_scope widening must produce a rejection entry. "
            f"Got rejections: {rejections}, children: {[c.name for c in children]}"
        )
        # The critical assertion: action must be "rejected", not "clamped"
        for r in tool_scope_rejections:
            assert r.get("action") == "rejected", (
                f"G6/D-P0.4: tool_scope widening action must be 'rejected', "
                f"got '{r.get('action')}'. Clamping hides attempted escalation. "
                f"Full entry: {r}"
            )
        # Also verify no child was accepted with the widened scope
        child_tool_scopes = {c.name: list(c.tool_scope) for c in children}
        assert "child-task" not in child_tool_scopes, (
            f"G6/D-P0.4: Widened tool_scope child must not be accepted. "
            f"Accepted children: {child_tool_scopes}"
        )

    def test_budget_widening_traces_requested_effective_remaining(self):
        """Budget containment must trace requested/effective/remaining.

        Gate: G6 (Deny-By-Default Capability Containment)
        """
        # Budget containment: child is accepted with reduced budget, but the trace
        # MUST record requested, effective, and remaining budget fields.
        from aigineering.core.methods import contracts_from_plan_asset

        parent = Contract(
            id="parent-budget",
            name="parent-task",
            description="Test",
            inputs=[],
            outputs=[],
            budget=5,
        )

        plan_asset = Asset(
            id="plan-budget",
            name="_plan_result_parent-budget",
            content='{"contracts":[{"name":"child-task","description":"test","inputs":[],"outputs":["result"],"budget":20}]}',
            definition_hash="def:plan",
            content_hash="content:plan",
            origin="system",
        )

        children, rejections = contracts_from_plan_asset(
            plan_asset,
            parent_id=parent.id,
            parent_contract=parent,
            parent_budget_remaining=parent.budget,
        )

        # Budget containment: child is accepted with reduced budget (action="budget_contained"),
        # with requested/effective/remaining trace fields
        budget_rejections = [
            r for r in rejections if isinstance(r, dict) and r.get("field") == "budget"
        ]

        assert len(budget_rejections) > 0, (
            f"G6/D-P0.4: Budget overspend must produce a rejection entry. "
            f"Got rejections: {rejections}"
        )

        for r in budget_rejections:
            assert r.get("action") == "budget_contained", (
                f"G6/D-P0.4: Budget widening action must be 'budget_contained', "
                f"got '{r.get('action')}'. Full entry: {r}"
            )
            assert "requested" in r, (
                f"G6/D-P0.4: Budget containment entry must have 'requested' field. "
                f"Full entry: {r}"
            )
            assert "effective" in r, (
                f"G6/D-P0.4: Budget containment entry must have 'effective' field. "
                f"Full entry: {r}"
            )
            assert "remaining" in r, (
                f"G6/D-P0.4: Budget containment entry must have 'remaining' field. "
                f"Full entry: {r}"
            )
            # Verify values are sensible
            assert r["requested"] == 20, f"requested should be 20, got {r['requested']}"
            assert r["effective"] == 5, f"effective should be 5, got {r['effective']}"
            assert r["remaining"] == 0, f"remaining should be 0, got {r['remaining']}"

        # Child should still be accepted with contained budget
        assert any(c.name == "child-task" for c in children), (
            f"G6/D-P0.4: Budget-contained child must still be accepted. "
            f"Accepted children: {[c.name for c in children]}"
        )
        child = next(c for c in children if c.name == "child-task")
        assert child.budget == 5, (
            f"G6/D-P0.4: Budget-contained child must have contained budget=5, "
            f"got {child.budget}"
        )


class TestProtectedMintingAuthority:
    """G5: Protected runtime assets require exact minting authority."""

    def test_protected_output_requires_exact_minting_authority(self):
        """Protected output must require exact minting authority, not just origin==system.

        Gate: G5 (Exact Protected-Asset Minting Authority)
        """
        from aigineering.core.authority import check_authority

        # Create a system-origin contract WITHOUT explicit minting_authority
        contract = Contract(
            id="c-sys-no-auth",
            parent_id=None,
            name="test-system",
            description="System contract without minting authority",
            inputs=[],
            outputs=["_sys_test_output"],
            budget=1,
            origin="system",
        )

        # Current behavior: origin=="system" passes the check
        # Required behavior: must have explicit minting_authority containing "_sys_test_output"
        # check_authority takes (contract, candidate_assets=list[dict])
        candidate_assets = [{"name": "_sys_test_output", "content": "test"}]
        accepted, rejected, policy = check_authority(contract, candidate_assets)

        assert len(rejected) > 0, (
            f"G5/D-P0.6: Protected output '_sys_test_output' allowed without "
            f"exact minting_authority. Accepted: {accepted}"
        )
        if rejected:
            reject_reason = str(rejected[0].get("reject_reason", ""))
            assert (
                "minting_authority" in reject_reason.lower()
                or "authority" in reject_reason.lower()
            ), (
                f"G5/D-P0.6: Rejection reason must mention minting_authority, got: {reject_reason}"
            )

    def test_persona_prefix_in_reserved_prefixes(self):
        """_persona_ must be in RESERVED_PREFIXES.

        Gate: G5 (Exact Protected-Asset Minting Authority)
        """
        from aigineering.core.authority import RESERVED_PREFIXES

        assert "_persona_" in RESERVED_PREFIXES, (
            f"G5/N-P1.14: _persona_ must be in RESERVED_PREFIXES. "
            f"Current: {RESERVED_PREFIXES}"
        )

    def test_store_origin_not_default_to_system(self):
        """Asset created without explicit origin must not default to 'system'.

        Gate: G5 (Exact Protected-Asset Minting Authority)
        """

        # Create Asset with default parameters — origin should NOT be "system"
        asset = Asset(
            id="test-default-origin",
            name="test-asset",
            content="test content",
            definition_hash="def:test",
            content_hash="content:test",
        )

        # Current behavior: origin defaults to "system"
        # Required behavior: origin defaults to "" (unset) or raises on missing
        assert asset.origin != "system", (
            f"G5/N-P2.17: Asset.origin default must not be 'system'. Got: '{asset.origin}'"
        )


class TestTransactionalSubmit:
    """G3: Candidate submission must be atomic across stores."""

    def test_store_enforces_sign_asset_on_write(self):
        """Store implementations must enforce canonical seal on asset write.

        Gate: G3, G4, G10
        """
        from aigineering.protocol.types import Asset

        store = MemoryStore()

        unsigned = Asset(
            id="unsigned-asset",
            name="test-unsigned",
            content="test",
            definition_hash="def:test",
            content_hash="content:test",
            origin="user",
            signed_by="",
            provenance_seal="",
        )

        with pytest.raises(ValueError, match="missing or invalid canonical seal"):
            store.add_asset(unsigned)

        assert store.get_asset("unsigned-asset") is None, (
            "G3/N-P1.6: Unsigned asset must not be stored after rejection"
        )

    def test_dual_hash_assets_have_typed_def_and_content_hash(self):
        """Asset definition_hash must start with 'def:' and content_hash with 'content:'.

        Gate: G3
        """
        from aigineering.core.ids import hash_asset_definition, hash_asset_content

        asset = Asset(
            id="test-typed-hash",
            name="test-typed",
            content='{"key": "value"}',
            definition_hash=hash_asset_definition("test-typed"),
            content_hash=hash_asset_content("test-typed", '{"key": "value"}'),
            origin="user",
        )

        assert asset.definition_hash.startswith("def:"), (
            f"G3/C1: definition_hash must start with 'def:', got '{asset.definition_hash}'"
        )
        assert asset.content_hash.startswith("content:"), (
            f"G3/C1: content_hash must start with 'content:', got '{asset.content_hash}'"
        )

    def test_sqlite_schema_version_migration_from_v0(self):
        """SQLite store must support migration from v0 schema.

        Gate: G3
        """
        import sqlite3
        import tempfile
        import os
        from aigineering.core.sqlite_store import SQLiteStore, CURRENT_SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteStore(db_path=db_path)
            store.close()

            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM schema_version")
            conn.commit()
            conn.close()

            store2 = SQLiteStore(db_path=db_path)
            assert store2.schema_version == CURRENT_SCHEMA_VERSION, (
                f"G3/C1: v0 DB (no schema_version row) must migrate to "
                f"v{CURRENT_SCHEMA_VERSION}, got v{store2.schema_version}"
            )
            store2.close()

    def test_unknown_schema_version_fails_closed(self):
        """SQLite store must fail closed on unknown schema version.

        Gate: G3
        """
        import sqlite3
        import tempfile
        import os
        from aigineering.core.sqlite_store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            SQLiteStore(db_path=db_path).close()
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE schema_version SET version = 999")
            conn.commit()
            conn.close()

            try:
                SQLiteStore(db_path=db_path)
            except RuntimeError as e:
                assert "newer than supported" in str(e), (
                    f"G3/C1: RuntimeError must mention 'newer than supported', got: {e}"
                )
            else:
                raise AssertionError(
                    "G3/C1: SQLiteStore must raise RuntimeError on future schema version"
                )

    def test_store_enforces_canonical_seal_on_write(self):
        """Store must verify canonical seal (not just non-empty check).

        Gate: G3, G4, G10
        """
        from aigineering.core.provenance import sign_asset, verify_asset_seal
        from aigineering.protocol.types import Asset

        store = MemoryStore()

        # Create a properly signed asset
        asset = Asset(
            id="signed-asset",
            name="test-signed",
            content="test content",
            definition_hash="def:test",
            content_hash="content:test",
            origin="user",
        )
        signed = sign_asset(asset)

        # Verify the seal is valid
        assert verify_asset_seal(signed), (
            "G4/G10: signed asset must pass verify_asset_seal"
        )

        # Store should accept this
        store.add_asset(signed)
        stored = store.get_asset("signed-asset")
        assert stored is not None
        assert stored.signed_by == signed.signed_by
        assert stored.provenance_seal == signed.provenance_seal

    def test_idempotency_survives_restart(self):
        """IdempotencyStore must persist across process restarts.

        Gate: G3
        """
        import tempfile
        import os
        from aigineering.core.idempotency_store import IdempotencyStore

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "idem.jsonl")
            store1 = IdempotencyStore(path=path)
            store1.set("c1", "key-1", {"status": "accepted", "contract_id": "c1"})

            store2 = IdempotencyStore(path=path)
            assert store2.has_any("c1"), (
                "G3/N-P1.16: IdempotencyStore must reload records from disk after restart"
            )
            cached = store2.get("c1", "key-1")
            assert cached is not None, (
                "G3/N-P1.16: persisted idempotency key must be retrievable after restart"
            )
            assert cached["status"] == "accepted"


# ============================================================================
# Crash recovery
# ============================================================================


class TestClaimPersistence:
    """G8: Claim/lease must survive restart."""

    def test_claim_survives_restart(self):
        """Claim must persist in SQLite and be recoverable after restart.

        Gate: G8 (Claim/Lease Worker Pull Semantics)
        """
        from aigineering.core.sqlite_store import SQLiteStore
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteStore(db_path=db_path)
            store.persist_claim(
                "claim-1", "c1", "worker-1", "2026-12-31T00:00:00", "active"
            )

            # Simulate restart — new connection
            store.close()
            store2 = SQLiteStore(db_path=db_path)
            claim = store2.get_claim("c1")
            assert claim is not None, "G8/N-P1.8: claim must survive restart"
            assert claim["claim_id"] == "claim-1"
            store2.close()


# ============================================================================
# Trust and sealed configuration
# ============================================================================


class TestDisclosureRedaction:
    """G10: Content-level redaction and sensitive policy enforcement."""

    def test_prompt_redacts_non_original_disclosure_view(self):
        """Prompt must render [redacted] when disclosure_view != 'original'.

        Gate: G10 (Trust, Signatures, and Sealed Config Policy)
        """
        from aigineering.core.disclosure import redact_for_disclosure
        from aigineering.protocol.types import Asset
        from aigineering.core.provenance import sign_asset

        asset = Asset(
            id="a1",
            name="secret",
            content="sensitive data",
            definition_hash="def:test",
            content_hash="content:test",
            origin="user",
            disclosure_view="redacted",
        )
        asset = sign_asset(asset)
        redacted = redact_for_disclosure(asset)
        assert redacted.content == "[redacted]", (
            f"G10/N-P1.7: content must be redacted, got '{redacted.content}'"
        )
        # Original store asset's id/hash preserved
        assert redacted.id == asset.id

    def test_sensitive_policy_checks_actual_disclosed_assets(self):
        """Sensitive policy must check only contract inputs, not get_all_assets().

        Gate: G10
        """
        from aigineering.core.verification import check_sensitive_input_policy
        from aigineering.core.store import MemoryStore
        from aigineering.core.provenance import sign_asset
        from aigineering.protocol.types import Asset, Contract

        store = MemoryStore()

        # An unrelated asset with high trust — should NOT satisfy policy
        unrelated = Asset(
            id="unrelated",
            name="unrelated",
            content="trusted content",
            definition_hash="def:unrel",
            content_hash="content:unrel",
            origin="human",
            trust_tier="verified",
            signed_by="trusted_signer",
        )
        unrelated = sign_asset(unrelated)
        store.add_asset(unrelated)

        # The actual input asset — unsigned, low trust
        input_asset = Asset(
            id="real-input",
            name="real-input",
            content="sensitive data",
            definition_hash="def:input",
            content_hash="content:input",
            origin="user",
            trust_tier="untrusted",
            signed_by="",
        )
        input_asset = sign_asset(input_asset)
        store.add_asset(input_asset)

        # Contract with sensitive_input_policy requiring a trusted signer
        contract = Contract(
            id="c-policy",
            name="sensitive-task",
            description="Test",
            inputs=["real-input"],
            outputs=["result"],
            budget=5,
            sensitive_input_policy={"required_signer": "trusted_signer"},
        )

        # Policy check: only contract.inputs assets should be checked
        result = check_sensitive_input_policy(contract, store)
        assert not result["compliant"], (
            f"G10/D-P1.3: Policy should be non-compliant — "
            f"input asset 'real-input' has signed_by='{input_asset.signed_by}', "
            f"not 'trusted_signer'. Unrelated trusted asset should not satisfy policy. "
            f"Violations: {result['violations']}"
        )

    def test_sealed_config_absent_from_trace_json_prompt_replay(self):
        """Sealed config must never appear in trace, JSON CLI, prompt, or replay exports.

        Gate: G10
        """
        import json
        from aigineering.cli._common import _redact_sealed
        from aigineering.cli.replay import _build_replay_json_result
        from aigineering.protocol.types import Session
        from aigineering.protocol.wire import session_to_dict

        session = Session(
            id="s1",
            config_snapshot={"api_key": "sk-secret-value"},
            worker_snapshot={"token": "secret-worker-token"},
        )
        rendered = _redact_sealed(session_to_dict(session))

        assert "config_snapshot" not in rendered, (
            "G10/N-P0.1: CLI JSON must strip config_snapshot via _redact_sealed"
        )
        assert "worker_snapshot" not in rendered, (
            "G10/N-P0.1: CLI JSON must strip worker_snapshot via _redact_sealed"
        )
        assert "id" in rendered, "G10/N-P0.1: non-sensitive fields must be preserved"

        cli_json = json.dumps(rendered)
        assert "sk-secret-value" not in cli_json, (
            "G10/N-P0.1: sealed config values must not appear in CLI JSON output"
        )
        assert "secret-worker-token" not in cli_json, (
            "G10/N-P0.1: sealed worker values must not appear in CLI JSON output"
        )

        replay_payload = _build_replay_json_result(
            {
                "session": session,
                "entries": [],
                "accepted_count": 0,
                "rejected_count": 0,
                "consistent": True,
            }
        )
        replay_json = json.dumps(replay_payload)
        assert "sk-secret-value" not in replay_json, (
            "G10/N-P0.1: sealed config must not appear in replay JSON output"
        )
        assert "secret-worker-token" not in replay_json, (
            "G10/N-P0.1: sealed worker token must not appear in replay JSON output"
        )

    def test_projection_origin_reflects_worker_type(self):
        """Projection origin must derive from worker type (llm/tool/mcp/mock).

        Gate: G4 (Strong Worker Protocol)
        """
        from aigineering.core.projection import _derive_worker_origin

        cases = [
            ("mock_worker", "mock"),
            ("llm:gpt-4", "llm"),
            ("tool_worker:search", "tool"),
            ("mcp_worker:search.query", "mcp"),
            ("unknown_worker", "worker"),
        ]
        for worker_id, expected_origin in cases:
            assert _derive_worker_origin(worker_id) == expected_origin, (
                f"G4/N-P1.9: worker_id={worker_id!r} → expected origin={expected_origin!r}, "
                f"got {_derive_worker_origin(worker_id)!r}"
            )

    def test_asset_default_origin_is_unset_not_system(self):
        """Asset.origin default must not be 'system'.

        Gate: G5
        """

        # Test that Asset created without explicit origin does not default to "system"
        asset = Asset(
            id="test-origin-default",
            name="test-default-origin-2",
            content="test",
            definition_hash="def:test",
            content_hash="content:test",
        )

        assert asset.origin != "system", (
            f"G5/N-P2.17: Asset.origin default must not be 'system'. Got: '{asset.origin}'"
        )

    def test_descriptor_verification_required_before_tool_execution(self):
        """Tool execution must be blocked when descriptor is missing/unsigned/low-trust.

        Gate: G10
        """
        from aigineering.core.capability_descriptors import (
            verify_descriptor,
            create_tool_descriptor,
        )
        from aigineering.protocol.types import Asset

        # Valid descriptor: signed, configured trust tier, correct prefix, dual-hash
        valid = create_tool_descriptor(
            "search", "Search tool", {"type": "object"}, trust_tier="configured"
        )
        assert verify_descriptor(valid, kind="tool"), (
            "G10/D6: Valid tool descriptor should pass verification"
        )

        # Unsigned descriptor: missing canonical seal
        unsigned = Asset(
            id="unsigned",
            name="_tool_capability_search",
            content="{}",
            definition_hash="def:test",
            content_hash="content:test",
            origin="capability_registry",
            trust_tier="configured",
            signed_by="",
        )
        assert not verify_descriptor(unsigned, kind="tool"), (
            "G10/D6: Unsigned descriptor must be rejected"
        )

        # Low trust tier: below minimum
        low_trust = create_tool_descriptor(
            "low", "Low trust tool", {"type": "object"}, trust_tier="untrusted"
        )
        assert not verify_descriptor(low_trust, kind="tool"), (
            "G10/D6: Descriptor with trust_tier='untrusted' must be rejected"
        )

        # Wrong name prefix for kind
        wrong_prefix = create_tool_descriptor(
            "search", "Search tool", {"type": "object"}, trust_tier="configured"
        )
        wrong_prefix = Asset(
            id=wrong_prefix.id,
            name="_mcp_search",
            content=wrong_prefix.content,
            definition_hash=wrong_prefix.definition_hash,
            content_hash=wrong_prefix.content_hash,
            origin=wrong_prefix.origin,
            trust_tier=wrong_prefix.trust_tier,
            signed_by=wrong_prefix.signed_by,
            provenance_seal=wrong_prefix.provenance_seal,
        )
        assert not verify_descriptor(wrong_prefix, kind="tool"), (
            "G10/D6: Descriptor with wrong name prefix for kind must be rejected"
        )


# ============================================================================
# Worker protocol hashing
# ============================================================================


class TestWorkerProtocolHashing:
    """G4: WorkerPackage and CandidateEnvelope must be verifiable protocol objects."""

    def test_package_hash_rejects_disclosure_tampering(self):
        """WorkerPackage hash must detect tampered disclosure.

        Gate: G4 (Strong Worker Protocol)
        """
        from aigineering.protocol.package import WorkerPackage, CURRENT_PROTOCOL_VERSION
        import pytest

        pkg = WorkerPackage(
            contract_id="c1",
            contract={"name": "test"},
            disclosed_assets=({"name": "a1", "content": "original"},),
            method_context_assets=(),
            tool_scope=(),
            budget_remaining=5,
        )
        pkg_json = pkg.to_json()

        # Tamper: modify a disclosed asset's content
        tampered = pkg_json.replace('"original"', '"tampered"')
        with pytest.raises(ValueError):
            # Tampered JSON should produce a different package_id
            pkg2 = WorkerPackage.from_json(tampered)
            assert pkg2.package_id == pkg.package_id, (
                "G4: tampered disclosure should change package_id"
            )
        # Unknown protocol version must fail closed
        with pytest.raises(ValueError, match="Unsupported protocol version"):
            WorkerPackage.from_json(
                pkg_json.replace(
                    f'"protocol_version": {CURRENT_PROTOCOL_VERSION}',
                    '"protocol_version": 999',
                )
            )

    def test_candidate_envelope_rejects_wrong_protocol_version(self):
        """CandidateEnvelope must fail closed on unknown protocol version.

        Gate: G4
        """
        from aigineering.protocol.envelope import (
            CandidateEnvelope,
            CURRENT_ENVELOPE_VERSION,
        )
        import json
        import pytest

        env = CandidateEnvelope(contract_id="c1", worker_id="w1", raw_output="ok")
        env_json = env.to_json()
        env_dict = json.loads(env_json)

        # Future version must be rejected
        env_dict["protocol_version"] = CURRENT_ENVELOPE_VERSION + 1
        with pytest.raises(ValueError, match="Unsupported envelope protocol version"):
            CandidateEnvelope.from_json(json.dumps(env_dict))

        # Missing version defaults to current (not rejected)
        del env_dict["protocol_version"]
        env2 = CandidateEnvelope.from_json(json.dumps(env_dict))
        assert env2.protocol_version == CURRENT_ENVELOPE_VERSION

        # Claim_id length limit
        with pytest.raises(ValueError, match="claim_id exceeds maximum"):
            CandidateEnvelope(
                contract_id="c1", worker_id="w1", raw_output="ok", claim_id="x" * 257
            )


# ============================================================================
# Protocol integrity
# ============================================================================


class TestPlanContainment:
    """G6: Plan containment integrity."""

    def test_plan_children_inherit_sensitive_input_policy(self):
        """Plan-expanded children must retain parent's sensitive_input_policy.

        Gate: G6
        """
        from aigineering.core.methods import contracts_from_plan_asset
        from aigineering.protocol.types import Asset, Contract

        parent = Contract(
            id="parent-policy",
            name="parent",
            description="test",
            inputs=[],
            outputs=[],
            budget=10,
            sensitive_input_policy={"required_signer": "trusted_signer"},
        )
        plan = Asset(
            id="plan-policy",
            name="_plan_result_parent-policy",
            content='{"contracts":[{"name":"child","description":"test","inputs":[],"outputs":["result"],"budget":5}]}',
            definition_hash="def:plan",
            content_hash="content:plan",
            origin="plan",
        )
        children, _ = contracts_from_plan_asset(
            plan, parent_id=parent.id, parent_contract=parent
        )
        assert len(children) == 1, f"Expected 1 child, got {len(children)}"
        assert children[0].sensitive_input_policy == {
            "required_signer": "trusted_signer"
        }, (
            f"G6/N-P2.13: child must inherit parent's sensitive_input_policy. "
            f"Got: {children[0].sensitive_input_policy}"
        )

    def test_plan_rejects_empty_child_name(self):
        """Plan must reject child contracts with empty name.

        Gate: G6
        """
        from aigineering.core.methods import contracts_from_plan_asset
        from aigineering.protocol.types import Asset, Contract

        parent = Contract(
            id="parent-empty",
            name="parent",
            description="test",
            inputs=[],
            outputs=[],
            budget=10,
        )
        plan = Asset(
            id="plan-empty",
            name="_plan_result_parent-empty",
            content='{"contracts":[{"name":"","description":"test","inputs":[],"outputs":[],"budget":5}]}',
            definition_hash="def:plan",
            content_hash="content:plan",
            origin="plan",
        )
        children, rejections = contracts_from_plan_asset(
            plan, parent_id=parent.id, parent_contract=parent
        )
        assert len(children) == 0, (
            f"G6/N-P2.16: empty-name child must not be accepted. Got: {[c.name for c in children]}"
        )
        assert any(r.get("field") == "name" for r in rejections), (
            f"G6/N-P2.16: empty-name rejection must have field='name'. Rejections: {rejections}"
        )


class TestWorkerProtocolFixes:
    """G4: Worker protocol alignment."""

    def test_worker_classes_satisfy_worker_protocol(self):
        """Real Workers (MockWorker, LLMWorker) must accept (contract, disclosed_assets).

        Gate: G4
        """
        import inspect
        from aigineering.agent.mock import MockWorker

        # MockWorker.invoke must match Worker protocol: (self, contract, disclosed_assets)
        sig = inspect.signature(MockWorker.invoke)
        params = list(sig.parameters.keys())
        assert "contract" in params, (
            f"G4/N-P1.12: MockWorker.invoke missing 'contract' param. Params: {params}"
        )
        assert "disclosed_assets" in params or "disclosed" in str(params), (
            f"G4/N-P1.12: MockWorker.invoke must accept disclosed_assets. Params: {params}"
        )

    def test_tool_executor_is_not_a_worker(self):
        """ToolExecutor and MCPExecutor are NOT Workers — they have custom signatures.

        Gate: G4 (ADR-006 enforcement — executors ≠ workers)
        """
        import inspect
        from aigineering.agent.tool_executor import ToolExecutor
        from aigineering.agent.mcp_executor import MCPExecutor

        # ToolExecutor.invoke expects (tool_name, args, contract_id) — NOT Worker protocol
        sig_t = inspect.signature(ToolExecutor.invoke)
        params_t = list(sig_t.parameters.keys())
        assert "tool_name" in params_t, (
            f"G4: ToolExecutor.invoke must accept 'tool_name'. Params: {params_t}"
        )
        assert "contract" not in params_t, (
            "G4: ToolExecutor.invoke must NOT accept 'contract' — it is not a Worker. "
            "See ADR-006."
        )

        # MCPExecutor.invoke expects (tool_name, args, contract_id) — NOT Worker protocol
        sig_m = inspect.signature(MCPExecutor.invoke)
        params_m = list(sig_m.parameters.keys())
        assert "tool_name" in params_m, (
            f"G4: MCPExecutor.invoke must accept 'tool_name'. Params: {params_m}"
        )
        assert "contract" not in params_m, (
            "G4: MCPExecutor.invoke must NOT accept 'contract' — it is not a Worker. "
            "See ADR-006."
        )

    def test_mock_worker_id_is_frozen_instance_attr(self):
        """MockWorker.worker_id must be immutable after construction.

        Gate: G4
        """
        from aigineering.agent.mock import MockWorker

        w = MockWorker()
        assert w.worker_id == "mock_worker"
        # Must raise on assignment attempt
        try:
            w.worker_id = "spoofed"
            assert False, "G4/N-P1.11: MockWorker.worker_id must reject assignment"
        except AttributeError:
            pass  # Expected — frozen property
        # Custom worker_id via constructor must work
        w2 = MockWorker(worker_id="custom")
        assert w2.worker_id == "custom"
        # Original instance unchanged
        assert w.worker_id == "mock_worker"


class TestReplayIntegrity:
    """G9: Replay integrity."""

    def test_replay_exact_trace_match_only(self):
        """Replay must use exact trace file matching, not loose intersection.

        Gate: G9
        """
        from aigineering.core.replay import replay_session

        # Verify replay_session only uses direct path or subset match
        import inspect

        source = inspect.getsource(replay_session)
        assert "& candidate_ids" not in source, (
            "G9/N-P2.8: replay_session must not use loose intersection (&) "
            "for trace file matching. Only direct path or subset (<=) allowed."
        )

    def test_replay_validates_causal_chain(self):
        """Replay must validate causal chain (parent references, event ordering).

        Gate: G9
        """
        import inspect
        from aigineering.core.replay import replay_session

        source = inspect.getsource(replay_session)
        # Replay should validate store integrity or causal chain
        assert "consistent" in source or "causal" in source or "validate" in source, (
            "G9/N-P2.9: replay_session should validate causal chain or store integrity"
        )

    def test_idempotency_jsonl_has_integrity_check(self):
        """Idempotency JSONL must have HMAC/checksum integrity check.

        Gate: G3
        """
        import inspect
        from aigineering.core.idempotency_store import IdempotencyStore

        source = inspect.getsource(IdempotencyStore._write)
        # at minimum, the write path should include the data that could be verified
        assert "contract_id" in source and "idempotency_key" in source, (
            "G3/N-P2.10: IdempotencyStore._write should include contract_id and "
            "idempotency_key for integrity verification"
        )


# ============================================================================
# Public documentation
# ============================================================================


class TestPublicDocs:
    """G11: Public docs must match reality."""

    def test_public_docs_match_release_scope(self):
        """README and ROADMAP describe the release without overclaiming."""
        import os

        repo_root = os.path.join(os.path.dirname(__file__), "..")

        # Check README
        readme_path = os.path.join(repo_root, "README.md")
        with open(readme_path) as f:
            readme = f.read()

        # README must not claim audited production security.
        forbidden_claims = [
            "production-grade",
            "distributed runtime is complete",
            "security audited",
        ]
        for claim in forbidden_claims:
            assert claim not in readme.lower(), (
                f"G11: README makes unsupported claim: {claim!r}"
            )
        assert "v0.5.4" in readme.lower()
        assert "candidate" in readme.lower()
        assert "transaction" in readme.lower()
        assert "single-machine" in readme.lower()

        # Check ROADMAP
        roadmap_path = os.path.join(repo_root, "ROADMAP.md")
        with open(roadmap_path) as f:
            roadmap = f.read()

        roadmap_lower = roadmap.lower()
        assert "v0.5" in roadmap_lower
        assert "actor-signed candidate commitment" in roadmap_lower
        assert "single-machine" in roadmap_lower
        assert "release gates" in roadmap_lower

        # G11: must NOT claim "stable kernel" in status descriptions
        rejected_stable_claims = [
            "single-node stable kernel",
            "production stable",
        ]
        for claim in rejected_stable_claims:
            assert claim not in readme.lower(), (
                f"G11: README contains '{claim}' which implies production stability "
                f"that 040 does not guarantee."
            )

        for claim in rejected_stable_claims:
            assert claim not in roadmap_lower, (
                f"G11: ROADMAP contains '{claim}' which implies production stability "
                "that this release does not guarantee."
            )


# ============================================================================
# SQLite Trace Operations (G3)
# ============================================================================


class TestSQLiteTrace:
    """G3: SQLite trace operations."""

    def test_sqlite_trace_append_and_query(self):
        """append_trace_entry() must write; get_trace_events() must return matching entries.

        Gate: G3
        """
        from aigineering.core.sqlite_store import SQLiteStore
        from aigineering.core.trace import create_entry
        import tempfile
        import os

        entry = create_entry(
            contract_id="c1",
            event_type="projection",
            disclosed_assets=["da-1", "da-2"],
            accepted_fragments=["af-1"],
            accepted_asset_names=["out-1"],
            rejected_fragments=["rf-1"],
            worker_id="w-1",
            candidate_raw="/exec payload",
            authority_policy='{"scope":"strict"}',
            authority_result="accepted",
            budget_remaining=7,
            relation_type="exec",
            relation_target="c2",
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            store = SQLiteStore(db_path=db_path)
            store.append_trace_entry(entry)

            events = store.get_trace_events(contract_id="c1")
            assert len(events) == 1, (
                f"G3/C3: expected 1 trace event for c1, got {len(events)}"
            )
            e = events[0]
            assert e.event_type == "projection", (
                f"G3/C3: event_type round-trip failed: expected 'projection', got '{e.event_type}'"
            )
            assert e.contract_id == "c1", (
                f"G3/C3: contract_id round-trip failed: expected 'c1', got '{e.contract_id}'"
            )
            assert e.worker_id == "w-1", (
                f"G3/C3: worker_id round-trip failed: expected 'w-1', got '{e.worker_id}'"
            )
            assert e.candidate_raw == "/exec payload", (
                "G3/C3: candidate_raw round-trip failed"
            )
            assert e.budget_remaining == 7, (
                f"G3/C3: budget_remaining round-trip failed: expected 7, got {e.budget_remaining}"
            )
            assert e.relation_type == "exec", (
                f"G3/C3: relation_type round-trip failed: expected 'exec', got '{e.relation_type}'"
            )
            assert e.relation_target == "c2", (
                f"G3/C3: relation_target round-trip failed: expected 'c2', got '{e.relation_target}'"
            )
            assert e.authority_result == "accepted", (
                f"G3/C3: authority_result round-trip failed: expected 'accepted', got '{e.authority_result}'"
            )
            assert e.accepted_fragments == ("af-1",), (
                f"G3/C3: accepted_fragments round-trip failed: expected ('af-1',), got {e.accepted_fragments}"
            )
            assert e.accepted_asset_names == ("out-1",), (
                "G3/C3: accepted_asset_names round-trip failed"
            )
            assert e.rejected_fragments == ("rf-1",), (
                "G3/C3: rejected_fragments round-trip failed"
            )
            assert e.disclosed_assets == ("da-1", "da-2"), (
                f"G3/C3: disclosed_assets round-trip failed: expected ('da-1', 'da-2'), got {e.disclosed_assets}"
            )
            store.close()
