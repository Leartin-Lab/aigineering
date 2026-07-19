"""Phase A: Failing tests for 050 runtime boundary refactoring.

These tests document the semantic drift that must be repaired before
v0.5.0 can ship.  Every test marked ``xfail(strict=True)`` asserts the
DESIRED post-refactor behavior — it currently FAILS because the runtime
does not yet enforce the invariant, and will turn GREEN as Phases B–G
repair the boundary.

Plan reference: .omo/plans/050-runtime-boundary-refactor-plan.md
"""

from __future__ import annotations

import pytest

from aigineering.core.store import MemoryStore
from aigineering.core.ids import hash_asset_content, hash_contract
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Contract

# ============================================================================
# W1 — Asset-driven parent completion (Plan §2.1, §4 Phase A item 1)
# ============================================================================


class TestTaskIdentityParentCollision:
    """hash_contract_v2 includes parent_id in contract identity per ADR-002.
    Two identical child definitions under different parents produce
    different contract IDs.
    """

    def test_identical_children_under_different_parents_have_distinct_ids(self):
        """Same child definition placed under different parents should
        produce different contract IDs via hash_contract_v2."""
        from aigineering.core.ids import hash_contract_v2

        kwargs = dict(
            name="child_work",
            description="do work",
            inputs=["input_x"],
            outputs=["output_y"],
            activation="input_x",
            budget=3,
            tool_scope=["read"],
            labels=["label1"],
            origin="human",
        )

        cid_under_parent_a = hash_contract_v2(**kwargs, parent_id="parent_aaa")
        cid_under_parent_b = hash_contract_v2(**kwargs, parent_id="parent_bbb")

        assert cid_under_parent_a != cid_under_parent_b, (
            f"Child contract IDs must differ across parents via hash_contract_v2. "
            f"parent_aaa: {cid_under_parent_a}, parent_bbb: {cid_under_parent_b}"
        )

        # Same parent_id, same params → same ID (deterministic)
        cid_a_again = hash_contract_v2(**kwargs, parent_id="parent_aaa")
        assert cid_under_parent_a == cid_a_again, (
            "hash_contract_v2 must be deterministic for same parent"
        )

        # Without parent_id (None), same params → same ID (legacy behavior)
        cid_no_parent_1 = hash_contract_v2(**kwargs, parent_id=None)
        cid_no_parent_2 = hash_contract_v2(**kwargs, parent_id=None)
        assert cid_no_parent_1 == cid_no_parent_2, (
            "Same params with parent_id=None should produce same ID"
        )

    def test_retry_contract_has_distinct_id(self):
        """Retry contracts bind the complete replacement security definition."""
        from aigineering.core.methods import retry_contract

        original_id = hash_contract(
            "retry_test",
            "",
            [],
            ["out"],
            "",
            3,
            [],
            [],
            "human",
        )
        original = Contract(
            id=original_id,
            name="retry_test",
            outputs=["out"],
            budget=3,
        )
        retry_id = retry_contract(original).id

        assert retry_id != original_id
        assert retry_id.startswith("task:v3:")
        assert retry_id == retry_contract(original).id, "must be deterministic"


# ============================================================================
# W6 — Claim lifecycle monotonicity (Plan §2.4, §4 Phase A item 4)
# ============================================================================


class TestClaimLifecycleMonotonicity:
    """Contract claims must be monotonic.  A claimed contract never returns
    to claimable — retry/recovery creates a new contract.

    The ClaimStore already enforces this at the claim level.  The engine-
    level gap (verified below) is that Engine.run() does not consult the
    claim store — it uses private `_completed` and `_suspended` sets
    without checking whether a contract has been previously claimed.
    """

    def test_claim_store_blocks_reclaim_of_claimed_contract(self):
        """ClaimStore prevents re-claiming a contract that has ever been
        claimed.  This is already enforced by `_claimed_contracts` set.
        """
        from aigineering.core.claims import ClaimStore

        store = ClaimStore()
        claim1 = store.claim("contract_x", "worker_1", lease_seconds=3600)

        assert claim1 is not None, "first claim should succeed"
        assert claim1.status == "active"

        # Re-claiming the same contract must return None
        claim2 = store.claim("contract_x", "worker_2", lease_seconds=60)
        assert claim2 is None, (
            "ClaimStore must block re-claim of previously claimed contract"
        )

    def test_submitted_claim_cannot_be_reclaimed(self):
        """After a claim transitions to 'submitted', the contract remains
        non-claimable.
        """
        from aigineering.core.claims import ClaimStore

        store = ClaimStore()
        claim = store.claim("contract_y", "worker_1", lease_seconds=3600)
        assert claim is not None

        submitted = store.submit_claim(claim.claim_id)
        assert submitted, "submit should succeed"

        # Contract still cannot be re-claimed
        claim2 = store.claim("contract_y", "worker_2", lease_seconds=60)
        assert claim2 is None, "Submitted claim should still prevent re-claiming"


# ============================================================================
# W8 — Protected asset minting (Plan §2.5, §4 Phase A item 5)
# ============================================================================


class TestProtectedAssetMinting:
    """Workers and public contracts must not mint assets under reserved
    runtime namespaces without explicit minting_authority.
    """

    def test_contract_rejected_when_declaring_reserved_output(self):
        """DESIRED: Creating a contract that declares a reserved output
        name should be REJECTED by the runtime ingress.

        RuntimeIngress.accept_contract() now enforces this at the
        ingress gate — the Contract data model remains a persistence
        concern and does not validate.
        """
        from aigineering.core.authority import RESERVED_PREFIXES
        from aigineering.core.runtime_ingress import RuntimeIngress

        reserved_name = "_sys_test_output"
        assert any(reserved_name.startswith(p) for p in RESERVED_PREFIXES)

        store = MemoryStore()
        trace_store = TraceStore()
        ingress = RuntimeIngress(store, trace_store)

        contract = Contract(
            id=hash_contract("bad", "", [], [reserved_name], "", 3, [], [], "human"),
            name="bad",
            inputs=[],
            outputs=[reserved_name],
            activation="",
            budget=3,
        )

        # RuntimeIngress.accept_contract() rejects reserved output names
        with pytest.raises(ValueError, match="protected prefix"):
            ingress.accept_contract(contract)

    def test_projection_rejects_reserved_output_from_worker(self):
        """DESIRED: Authority check rejects reserved asset names from worker
        candidates even when they appear in contract.outputs, unless
        minting_authority is explicitly granted.

        This test currently PASSES because projection already checks
        reserved prefixes.  The gap documented is: this check is in
        projection.py, NOT in a unified RuntimeIngress.  If a new path
        bypasses projection and writes directly to store, reserved names
        could leak through.
        """
        from aigineering.core.projection import project_candidate
        from aigineering.protocol.types import Candidate

        contract = Contract(
            id=hash_contract(
                "worker_test",
                "",
                [],
                ["_tool_obs_test"],
                "",
                3,
                [],
                [],
                "human",
            ),
            name="worker_test",
            inputs=[],
            outputs=["_tool_obs_test"],
            activation="",
            budget=3,
        )

        candidate = Candidate(
            worker_id="test_worker",
            raw_output="_tool_obs_test: malicious content",
        )

        result = project_candidate(contract, candidate)

        accepted_names = {a.name for a in result.accepted_assets}
        assert "_tool_obs_test" not in accepted_names, (
            f"Reserved asset '_tool_obs_test' should be REJECTED without "
            f"minting_authority.  Accepted: {accepted_names}"
        )

    def test_direct_store_write_bypasses_reserved_name_check(self):
        """DESIRED: Writing a reserved-name asset through the runtime
        ingress should be blocked — only RuntimeIngress with
        allow_protected=True should permit it.

        RuntimeIngress.accept_asset() now enforces the protected-name
        check at the ingress gate.
        """
        from aigineering.core.runtime_ingress import RuntimeIngress

        store = MemoryStore()
        trace_store = TraceStore()
        ingress = RuntimeIngress(store, trace_store)

        asset = Asset(
            id=hash_asset_content("_sys_test", "hack"),
            name="_sys_test",
            content="hack",
        )

        # RuntimeIngress.accept_asset() rejects protected names by default.
        # Only allow_protected=True permits them.
        with pytest.raises(ValueError, match="collides with reserved prefix"):
            ingress.accept_asset(asset)


# ============================================================================
# W5 — Tool/MCP observation cannot satisfy declared outputs (Plan §2.6)
# ============================================================================


class TestDirectWriteBan:
    """Production modules must not call ``store.add_asset`` or
    ``store.add_contract`` directly.  Allowed exceptions:
    store implementations, transaction helpers, RuntimeIngress, and
    explicit test fixtures.
    """

    # Modules allowed to call add_asset/add_contract directly.
    _ALLOWED: frozenset[str] = frozenset(
        {
            "store.py",
            "sqlite_store.py",
            "runtime_ingress.py",
        }
    )

    def test_no_direct_store_write_in_production(self):
        """Scan src/aigineering/ for direct store.write calls in production
        modules.  Any violation outside the allowlist is a P0 architecture
        gap.
        """
        import ast
        from pathlib import Path

        src_root = Path(__file__).parent.parent / "src" / "aigineering"
        violations: list[str] = []

        for py_file in src_root.rglob("*.py"):
            rel = py_file.relative_to(src_root).as_posix()

            # Skip tests, pycache, and allowed modules
            if "test_" in rel or "__pycache__" in rel:
                continue
            if any(rel.endswith(allowed) for allowed in self._ALLOWED):
                continue

            source = py_file.read_text()
            tree = ast.parse(source)

            # Check for direct attribute access: something.add_asset(...)
            # or something.add_contract(...)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        method_name = node.func.attr
                        if method_name in ("add_asset", "add_contract"):
                            # Check if this is a self._store or store.* call
                            if isinstance(node.func.value, ast.Attribute):
                                inner = node.func.value.attr
                                if inner in ("_store", "store"):
                                    violations.append(
                                        f"{rel}:{node.lineno}  {method_name}()"
                                    )
                            elif isinstance(node.func.value, ast.Name):
                                inner = node.func.value.id
                                if inner in ("store", "_store"):
                                    violations.append(
                                        f"{rel}:{node.lineno}  {method_name}()"
                                    )

        assert len(violations) == 0, (
            f"Direct store writes found outside allowlist "
            f"({sorted(self._ALLOWED)}):\n" + "\n".join(f"  - {v}" for v in violations)
        )
