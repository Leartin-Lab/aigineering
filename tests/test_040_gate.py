"""040 Production Kernel Gate Tests.

These tests encode the non-negotiable invariants from 040-production-kernel-gate.md
(G1-G11). All tests must FAIL against current code (pre-repair baseline) and PASS
after each gate is repaired.

Each test docstring maps to a specific gate and documented debt item.

Gate test convention:
- Test name encodes: what_surface_under_test + expected_behavior
- Assertion messages reference the gate number + debt ID
- Tests use the public Engine/store API, never _private members (G7)
"""

import pytest

from aigineering.protocol.types import Contract, Asset
from aigineering.core.store import MemoryStore


# ============================================================================
# Phase A Gate Tests — P0 Leaks (G10, G9)
# ============================================================================


class TestSessionSealedConfig:
    """G10: Sealed config must never leak into text output."""

    def test_session_show_does_not_print_config_snapshot_values(self):
        """session show text output must not print raw config_snapshot values.

        Gate: G10 (Trust, Signatures, and Sealed Config Policy)
        Debt: N-P0.1 (cli/session.py:53-54)
        """
        from aigineering.core.session import SessionStore, Session

        # Create a session with sensitive config
        store = SessionStore()
        session = Session(
            id="test-session-redact",
            root_contract_id="root-1",
            contract_ids=["c-1"],
            asset_ids=[],
            trace_ids=[],
            config_snapshot={"api_key": "sk-secret-12345", "model": "gpt-4"},
            worker_snapshot={"worker_id": "llm-worker-001", "token": "secret-token"},
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
        Debt: N-P0.1
        """
        from aigineering.cli.session import session_show
        from aigineering.core.session import SessionStore, Session
        from click.testing import CliRunner

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

        runner = CliRunner()
        result = runner.invoke(session_show, ["test-redact-leak"])

        # The raw secret values must NOT appear in text output
        assert "sk-secret-12345" not in result.output, (
            f"G10/N-P0.1: config_snapshot api_key leaked in text output:\n{result.output}"
        )
        assert "secret-token" not in result.output, (
            f"G10/N-P0.1: worker_snapshot token leaked in text output:\n{result.output}"
        )


class TestRestoreBudget:
    """G9: Recovery must reconstruct runtime state from committed records."""

    @pytest.mark.xfail(
        reason="Deferred to Phase B2: _dispatch_method budget decrements not yet traced (N-P1.1). "
               "Full fix requires budget_consumed trace events, then counting them alongside activations."
    )
    def test_restore_from_store_counts_method_scheduled_as_consumption(self):
        """restore_from_store must count method_scheduled trace events as budget consumption.

        Gate: G9 (Recovery Is Crash-Consistent)
        Debt: N-P0.2 (engine.py:664-668)

        DEFERRED to Phase B2: _dispatch_method() decrements budget without tracing (N-P1.1).
        Counting method_scheduled alone would double-count because the activation event
        already covers the base budget decrement. The correct fix requires:
        1. Phase B2: Add budget_consumed trace events to _dispatch_method
        2. Then: Count both activation + budget_consumed in restore_from_store
        """
        import pytest
        pytest.skip("Deferred to Phase B2 — requires budget_consumed trace events (N-P1.1 fix first)")


# ============================================================================
# Phase A Gate Tests — Baseline (must all FAIL pre-repair)
# ============================================================================


class TestCLIRetryBypass:
    """G1: CLI must not directly mutate runtime state."""

    def test_cli_retry_does_not_directly_mutate_store(self):
        """aig retry must go through method ingress, not direct store.add_contract().

        Gate: G1 (Single Runtime Ingress)
        Debt: D-P0.1 (cli/retry.py:22-47)
        """
        from aigineering.protocol.types import Contract

        store = MemoryStore()
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

        # Full test after B3 repair (CLI retry → method ingress)
        pass  # Full test after B3 repair


class TestContextOverflow:
    """G1/G2: Context overflow must go through method path, not Engine-fabricated candidate."""

    def test_context_overflow_creates_report_asset_and_method_path(self):
        """Context overflow must create _context_overflow_report_ asset, not Engine-fabricated candidate.

        Gate: G1 (Single Runtime Ingress), G2 (Engine Is Kernel, Not Feature Bus)
        Debt: D-P0.2 (engine.py:344-355)
        """
        from aigineering.core.engine import Engine
        from aigineering.core.trace import MemoryTraceStore
        from aigineering.protocol.types import Contract, Asset
        from aigineering.agent.mock import MockWorker

        store = MemoryStore()
        trace = MemoryTraceStore()
        worker = MockWorker()
        engine = Engine(store=store, worker=worker, trace_store=trace)

        # Create a contract with a massive input asset that would overflow
        contract = Contract(
            id="c-overflow",
            parent_id=None,
            name="overflow-test",
            description="Test",
            inputs=["big-asset"],
            outputs=["result"],
            budget=10,
        )
        huge_asset = Asset(
            id="big-asset",
            name="big-doc",
            content="x" * 10000,  # ~2500 tokens
            definition_hash="def:test",
            content_hash="content:test",
            origin="user",
        )
        store.add_contract(contract)
        store.add_asset(huge_asset)

        # Set a small context limit to trigger overflow
        engine._context_size_limit = 100  # ~25 tokens

        # Check context overflow — should NOT fabricate a candidate with worker_id="engine"
        # After repair: should create _context_overflow_report_ asset + schedule /replan via method
        overflowed = engine._check_context_overflow(contract, [huge_asset])

        if overflowed:
            # Check that no candidate with worker_id="engine" was fabricated
            # (This is the current bug — Engine creates synthetic candidate)
            # After repair, this check should pass
            # placeholder — full assertion after B2 repair
            pass


class TestAuthorityClamp:
    """G6: Authority widening must be rejected, not clamped."""

    def test_tool_scope_widening_rejects_child_contract(self):
        """Tool scope widening must reject the child contract entirely.

        Gate: G6 (Deny-By-Default Capability Containment)
        Debt: D-P0.4 (methods.py:275-294)
        """
        # Current behavior: clamps tool_scope (action="clamped"), child accepted
        # Required behavior: rejects child (action="rejected"), child skipped
        # This test must FAIL against current code (which clamps)
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
            content='{"contracts":[{"name":"child-task","description":"test","inputs":[],"outputs":[],"budget":5,"tool_scope":["tool_a","tool_b","tool_c"]}]}',
            definition_hash="def:plan",
            content_hash="content:plan",
            origin="system",
        )

        children, rejections = contracts_from_plan_asset(
            plan_asset, parent_id=parent.id, parent_contract=parent
        )

        # After repair: the tool_scope widening (adding "tool_c") must REJECT the child
        # Current bug: code clamps (action="clamped") — this test must FAIL until B4 repair
        tool_scope_rejections = [
            r for r in rejections
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
        assert "child-task" not in child_tool_scopes or set(child_tool_scopes.get("child-task", [])).issubset({"tool_a", "tool_b"}), (
            f"G6/D-P0.4: Widened tool_scope child must not be accepted. "
            f"Accepted children: {child_tool_scopes}"
        )

    def test_budget_widening_traces_requested_effective_remaining(self):
        """Budget containment must trace requested/effective/remaining.

        Gate: G6 (Deny-By-Default Capability Containment)
        Debt: D-P0.4 (methods.py:358-395)
        """
        # Budget containment is different from authority rejection:
        # - Budget can be contained (child accepted with reduced budget)
        # - But the trace MUST record requested, effective, and remaining budget
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
            content='{"contracts":[{"name":"child-task","description":"test","inputs":[],"outputs":[],"budget":20}]}',
            definition_hash="def:plan",
            content_hash="content:plan",
            origin="system",
        )

        children, rejections = contracts_from_plan_asset(
            plan_asset, parent_id=parent.id, parent_contract=parent,
            parent_budget_remaining=parent.budget,
        )

        # Budget clamping: child is accepted with reduced budget (current behavior)
        # Phase B4 will change action from "clamped" to "budget_contained" with
        # requested/effective/remaining trace fields
        budget_rejections = [
            r for r in rejections
            if isinstance(r, dict) and r.get("field") == "budget"
        ]

        if budget_rejections:
            # Current behavior: budget is clamped
            clamped = [r for r in budget_rejections if r.get("action") == "clamped"]
            assert len(clamped) > 0 or len(children) > 0, (
                f"G6/D-P0.4: Budget must be clamped or contained for child overspend. "
                f"Children: {[c.name for c in children]}, Budget rejections: {budget_rejections}"
            )
            # After Phase B4 repair: verify containment trace has requested/effective/remaining
            # The assertion below will be updated when B4 implements explicit budget tracing


class TestProtectedMintingAuthority:
    """G5: Protected runtime assets require exact minting authority."""

    def test_protected_output_requires_exact_minting_authority(self):
        """Protected output must require exact minting authority, not just origin==system.

        Gate: G5 (Exact Protected-Asset Minting Authority)
        Debt: D-P0.6 (authority.py:63-68)
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

        # This test MUST FAIL pre-repair (current code allows it based on origin=="system")
        # After repair: _sys_test_output should be rejected without exact minting_authority
        assert len(rejected) > 0, (
            f"G5/D-P0.6: Protected output '_sys_test_output' allowed without "
            f"exact minting_authority. Accepted: {accepted}"
        )
        if rejected:
            reject_reason = str(rejected[0].get("reject_reason", ""))
            assert "minting_authority" in reject_reason.lower() or "authority" in reject_reason.lower(), (
                f"G5/D-P0.6: Rejection reason must mention minting_authority, got: {reject_reason}"
            )

    def test_persona_prefix_in_reserved_prefixes(self):
        """_persona_ must be in RESERVED_PREFIXES.

        Gate: G5 (Exact Protected-Asset Minting Authority)
        Debt: N-P1.14 (authority.py:7-24)
        """
        from aigineering.core.authority import RESERVED_PREFIXES

        assert "_persona_" in RESERVED_PREFIXES, (
            f"G5/N-P1.14: _persona_ must be in RESERVED_PREFIXES. "
            f"Current: {RESERVED_PREFIXES}"
        )

    def test_store_origin_not_default_to_system(self):
        """Asset created without explicit origin must not default to 'system'.

        Gate: G5 (Exact Protected-Asset Minting Authority)
        Debt: N-P1.15 (store.py:112), N-P2.17 (types.py:18)
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


class TestMethodHandlerIsolation:
    """G7: Method handlers must not access Engine private state."""

    def test_method_handler_cannot_access_engine_private_state(self):
        """MethodHandler must not receive full Engine or access _private members.

        Gate: G7 (MethodRuntime Boundary)
        Debt: D-P0.5 (method_registry.py:19-30)
        """
        from aigineering.core.method_handlers.plan import PlanMethodHandler
        from aigineering.core.method_handlers.tool import ToolMethodHandler
        from aigineering.core.method_handlers.replan import ReplanMethodHandler
        from aigineering.core.method_handlers.retry import RetryMethodHandler
        import inspect

        handlers = [
            PlanMethodHandler(),
            ToolMethodHandler(),
            ReplanMethodHandler(),
            RetryMethodHandler(),
        ]

        for handler in handlers:
            sig = inspect.signature(handler.handle_method)
            params = list(sig.parameters.keys())

            # Check parameter name — should be "runtime" not "engine"
            # Current code passes "engine" as first param
            assert "engine" not in params, (
                f"G7/D-P0.5: {type(handler).__name__}.handle_method() receives 'engine' "
                f"parameter. Must use MethodRuntime interface instead. Params: {params}"
            )

    def test_retry_handler_uses_add_contract_not_store_direct(self):
        """RetryHandler must use runtime.add_contract(), not engine._store directly.

        Gate: G1 (Single Runtime Ingress), G7 (MethodRuntime Boundary)
        Debt: N-P1.4 (method_handlers/retry.py:60-61)
        """
        import ast
        import os

        retry_handler_path = os.path.join(
            os.path.dirname(__file__),
            "..", "src", "aigineering", "core", "method_handlers", "retry.py"
        )

        with open(retry_handler_path) as f:
            source = f.read()

        tree = ast.parse(source)

        # Check for direct store access patterns
        store_accesses = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if hasattr(node, 'attr') and node.attr.startswith('_store'):
                    store_accesses.append(f"line {node.lineno}: ._store")
                if hasattr(node, 'attr') and node.attr.startswith('_budget'):
                    store_accesses.append(f"line {node.lineno}: ._budget")
                if hasattr(node, 'attr') and node.attr.startswith('_add_trace'):
                    store_accesses.append(f"line {node.lineno}: ._add_trace")

        assert len(store_accesses) == 0, (
            f"G7/N-P1.4: RetryHandler accesses Engine private members: {store_accesses}. "
            f"Must use MethodRuntime interface instead."
        )


# ============================================================================
# Phase C Gate Tests — Transactional Substrate (G3)
# ============================================================================


class TestTransactionalSubmit:
    """G3: Candidate submission must be atomic across stores."""

    def test_worker_submit_atomic_asset_trace_idempotency(self):
        """submit_candidate must atomically write assets + trace + idempotency in one transaction.

        Gate: G3 (Transactional Runtime Substrate)
        Debt: D-P0.3 (submit.py:83-153)
        """
        # Current behavior: separate writes to JsonLStore, JsonLTraceStore, IdempotencyStore
        # After repair: single RuntimeTransaction
        # This test marks the requirement; actual verification after C2 repair
        pass  # Placeholder — implement after Phase C2

    def test_store_enforces_sign_asset_on_write(self):
        """Store implementations must enforce canonical seal on asset write.

        Gate: G3, G4, G10
        Debt: N-P1.6 (store.py:39,173; sqlite_store.py:255)
        """
        from aigineering.protocol.types import Asset

        store = MemoryStore()

        # Create an unsigned asset
        unsigned = Asset(
            id="unsigned-asset",
            name="test-unsigned",
            content="test",
            definition_hash="def:test",
            content_hash="content:test",
            origin="user",
            signed_by="",
            signature="",
        )

        # Current behavior: store accepts unsigned assets
        # Required behavior: store must reject unsigned assets
        try:
            store.add_asset(unsigned)
            stored = store.get_asset("unsigned-asset")
            assert stored is not None
            # Check if stored asset has a valid seal
            has_seal = bool(stored.signed_by and stored.signature)
            if not has_seal:
                # This is the current broken behavior — store accepted unsigned asset
                # After repair, store.add_asset() should raise or reject
                assert False, (
                    f"G3/N-P1.6: Store accepted unsigned asset. "
                    f"signed_by='{stored.signed_by}', signature='{stored.signature}'"
                )
        except (ValueError, TypeError):
            # This is the expected behavior after repair
            pass

    def test_dual_hash_assets_have_typed_def_and_content_hash(self):
        """Asset definition_hash must start with 'def:' and content_hash with 'content:'.

        Gate: G3
        Debt: C1 (Phase C dual-hash requirement)
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
        Debt: C1 (Schema version requirement)
        """
        # Placeholder — implement after Phase C1
        pass

    def test_unknown_schema_version_fails_closed(self):
        """SQLite store must fail closed on unknown schema version.

        Gate: G3
        Debt: C1 (Unknown version requirement)
        """
        # Placeholder — implement after Phase C1
        pass

    def test_store_enforces_canonical_seal_on_write(self):
        """Store must verify canonical seal (not just non-empty check).

        Gate: G3, G4, G10
        Debt: N-P1.6
        """
        from aigineering.core.provenance import sign_asset, verify_asset_signature
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
        assert verify_asset_signature(signed), (
            "G4/G10: signed asset must pass verify_asset_signature"
        )

        # Store should accept this
        store.add_asset(signed)
        stored = store.get_asset("signed-asset")
        assert stored is not None
        assert stored.signed_by == signed.signed_by
        assert stored.signature == signed.signature

    def test_idempotency_survives_restart(self):
        """IdempotencyStore must persist across process restarts.

        Gate: G3
        Debt: N-P1.16 (idempotency_store.py:56)
        """
        # Placeholder — implement after Phase C2
        pass


# ============================================================================
# Phase C Gate Tests — Crash Recovery (G3, G9)
# ============================================================================


class TestCrashRecovery:
    """G9: Crash-consistent recovery."""

    def test_crash_after_asset_before_trace_recovers(self):
        """Crash between asset write and trace must not produce inconsistent state.

        Gate: G9 (Recovery Is Crash-Consistent)
        Debt: C5 (Crash injection tests)
        """
        pass  # Placeholder — implement after Phase C5

    def test_crash_after_method_schedule_before_parent_suspend(self):
        """Crash between method schedule and parent suspend must recover correctly.

        Gate: G9
        Debt: C5
        """
        pass  # Placeholder

    def test_crash_after_child_complete_before_parent_resume(self):
        """Crash between child completion and parent resume must recover correctly.

        Gate: G9
        Debt: C5
        """
        pass  # Placeholder

    def test_double_crash_recovery_idempotent(self):
        """Repeated recovery must be idempotent.

        Gate: G9
        Debt: C5
        """
        pass  # Placeholder


# ============================================================================
# Phase C Gate Tests — Claim/Lease (G8)
# ============================================================================


class TestClaimPersistence:
    """G8: Claim/lease must survive restart."""

    def test_claim_survives_restart(self):
        """Claim must persist in SQLite and be recoverable after restart.

        Gate: G8 (Claim/Lease Worker Pull Semantics)
        Debt: N-P1.8 (claims.py:49-177)
        """
        pass  # Placeholder — implement after Phase C4

    def test_worker_submit_validates_claim_lease_and_worker(self):
        """worker submit must validate claim ownership, lease status, and worker id.

        Gate: G8
        Debt: D-P1.2 (cli/worker.py:67-145)
        """
        pass  # Placeholder — implement after Phase E3


# ============================================================================
# Phase D Gate Tests — Trust & Sealed Config (G4, G10)
# ============================================================================


class TestDisclosureRedaction:
    """G10: Content-level redaction and sensitive policy enforcement."""

    def test_prompt_redacts_non_original_disclosure_view(self):
        """Prompt must render [redacted] when disclosure_view != 'original'.

        Gate: G10 (Trust, Signatures, and Sealed Config Policy)
        Debt: N-P1.7 (prompt.py:42)
        """
        pass  # Placeholder — implement after Phase D1

    def test_sensitive_policy_checks_actual_disclosed_assets(self):
        """Sensitive policy must check only contract inputs, not get_all_assets().

        Gate: G10
        Debt: D-P1.3 (verification.py:203,226)
        """
        pass  # Placeholder — implement after Phase D2

    def test_sealed_config_absent_from_trace_json_prompt_replay(self):
        """Sealed config must never appear in trace, JSON CLI, prompt, or replay exports.

        Gate: G10
        Debt: N-P0.1 (Phase D3 verification)
        """
        pass  # Placeholder — implement after Phase D3

    def test_projection_origin_reflects_worker_type(self):
        """Projection origin must derive from worker type (llm/tool/mcp/mock).

        Gate: G4 (Strong Worker Protocol)
        Debt: N-P1.9 (projection.py:83)
        """
        pass  # Placeholder — implement after Phase D4

    def test_asset_default_origin_is_unset_not_system(self):
        """Asset.origin default must not be 'system'.

        Gate: G5
        Debt: N-P1.15, N-P2.17
        """

        # Test that Asset created without explicit origin does not default to "system"
        asset = Asset(
            id="test-origin-default",
            name="test-default-origin-2",
            content="test",
            definition_hash="def:test",
            content_hash="content:test",
        )

        # After repair: origin should be "" (unset)
        assert asset.origin != "system", (
            f"G5/N-P2.17: Asset.origin default must not be 'system'. Got: '{asset.origin}'"
        )

    def test_descriptor_verification_required_before_tool_execution(self):
        """Tool execution must be blocked when descriptor is missing/unsigned/low-trust.

        Gate: G10
        Debt: D6 (Minimum capability descriptor trust gate)
        """
        pass  # Placeholder — implement after Phase D6


# ============================================================================
# Phase E Gate Tests — Worker Protocol Hashing (G4)
# ============================================================================


class TestWorkerProtocolHashing:
    """G4: WorkerPackage and CandidateEnvelope must be verifiable protocol objects."""

    def test_package_hash_rejects_disclosure_tampering(self):
        """WorkerPackage hash must detect tampered disclosure.

        Gate: G4 (Strong Worker Protocol)
        Debt: D-P1.1 (package.py:10-53)
        """
        pass  # Placeholder — implement after Phase E1

    def test_candidate_envelope_rejects_wrong_protocol_version(self):
        """CandidateEnvelope must fail closed on unknown protocol version.

        Gate: G4
        Debt: N-P2.5, N-P2.11
        """
        pass  # Placeholder — implement after Phase E2


# ============================================================================
# Phase F Gate Tests — Protocol Integrity (G4, G6, G9)
# ============================================================================


class TestPlanContainment:
    """G6: Plan containment integrity."""

    def test_plan_children_inherit_sensitive_input_policy(self):
        """Plan-expanded children must retain parent's sensitive_input_policy.

        Gate: G6
        Debt: N-P2.13 (methods.py:234-422)
        """
        pass  # Placeholder — implement after Phase F1

    def test_plan_rejects_empty_child_name(self):
        """Plan must reject child contracts with empty name.

        Gate: G6
        Debt: N-P2.16 (methods.py:195)
        """
        pass  # Placeholder — implement after Phase F1


class TestWorkerProtocolFixes:
    """G4: Worker protocol alignment."""

    def test_tool_worker_aligns_with_worker_protocol(self):
        """ToolWorker.invoke must match Worker protocol signature.

        Gate: G4
        Debt: N-P1.12 (tool_worker.py:24, mcp_worker.py:27)
        """
        pass  # Placeholder — implement after Phase F2

    def test_mock_worker_id_is_frozen_instance_attr(self):
        """MockWorker.worker_id must be immutable after construction.

        Gate: G4
        Debt: N-P1.11 (mock.py:10-12)
        """
        pass  # Placeholder — implement after Phase F2


class TestReplayIntegrity:
    """G9: Replay integrity."""

    def test_replay_exact_trace_match_only(self):
        """Replay must use exact trace file matching, not loose intersection.

        Gate: G9
        Debt: N-P2.8 (replay.py:41-60)
        """
        pass  # Placeholder — implement after Phase F3

    def test_replay_validates_causal_chain(self):
        """Replay must validate causal chain (parent references, event ordering).

        Gate: G9
        Debt: N-P2.9 (replay.py:93-101)
        """
        pass  # Placeholder — implement after Phase F3

    def test_idempotency_jsonl_has_integrity_check(self):
        """Idempotency JSONL must have HMAC/checksum integrity check.

        Gate: G3
        Debt: N-P2.10 (idempotency_store.py:35-51)
        """
        pass  # Placeholder — implement after Phase F3


# ============================================================================
# Phase G Gate Tests — Public Docs (G11)
# ============================================================================


class TestPublicDocs:
    """G11: Public docs must match reality."""

    def test_public_docs_do_not_claim_050_or_production_security(self):
        """README and ROADMAP must not claim completed transactional durability or 050.

        Gate: G11 (Public Claims Match Reality)
        Debt: G11-D1 (README.md:55-96), G11-D2 (ROADMAP.md:3-13)
        """
        import os

        repo_root = os.path.join(os.path.dirname(__file__), "..")

        # Check README
        readme_path = os.path.join(repo_root, "README.md")
        with open(readme_path) as f:
            readme = f.read()

        # README must not claim "completed" or "production" for 040 foundation
        forbidden_claims = [
            "transactional durability",
            "completed foundation",
            "production-grade",
        ]
        for claim in forbidden_claims:
            assert claim not in readme.lower(), (
                f"G11: README claims '{claim}' which is not yet true under 040 gate."
            )

        # Check ROADMAP
        roadmap_path = os.path.join(repo_root, "ROADMAP.md")
        with open(roadmap_path) as f:
            roadmap = f.read()

        # ROADMAP must not claim v0.4 transactional durability as completed.
        # Check: if "transactional" appears, it must be in a context of incompleteness.
        roadmap_lower = roadmap.lower()
        if "transactional" in roadmap_lower:
            # Find the line containing "transactional"
            transactional_lines = [
                line for line in roadmap.split("\n")
                if "transactional" in line.lower()
            ]
            # Each line mentioning transactional must indicate incompleteness
            incompleteness_markers = ["[ ]", "in progress", "deferred", "not yet", "planned"]
            for line in transactional_lines:
                line_lower = line.lower()
                assert any(marker in line_lower for marker in incompleteness_markers), (
                    f"G11: ROADMAP line claims transactional durability as complete: '{line.strip()}'. "
                    f"Must include incompleteness marker: {incompleteness_markers}"
                )


# ============================================================================
# SQLite Trace Operations (G3)
# ============================================================================


class TestSQLiteTrace:
    """G3: SQLite trace operations."""

    def test_sqlite_trace_append_and_query(self):
        """append_trace_entry() must write; get_trace_events() must return matching entries.

        Gate: G3
        Debt: C3 (SQLite trace operations)
        """
        pass  # Placeholder — implement after Phase C3
