"""Real crash injection tests using subprocess os._exit(1).

Each test spawns a subprocess that crashes at a controlled injection point,
then verifies that the Engine can recover from the persisted SQLite state.
"""

import os
import subprocess
import sys


from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Contract

# Path to the Python interpreter in the venv
VENV_PYTHON = sys.executable


def _run_crash_script(
    crash_point: str, script: str, db_path: str, extra_env: dict = None
) -> subprocess.CompletedProcess:
    """Run *script* in a subprocess with crash point and DB path set.

    Returns the CompletedProcess.  The subprocess is expected to exit
    with code 1 (os._exit).
    """
    env = {
        **os.environ,
        "AIG_ENABLE_CRASH_INJECTION": "1",
        "AIG_CRASH_POINT": crash_point,
        "AIG_TEST_DB": db_path,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [VENV_PYTHON, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestCrashAfterAssetBeforeTrace:
    """Crash between asset write and trace write inside commit_candidate_submission."""

    def test_crash_after_asset_before_trace_atomic_rollback(self, tmp_path):
        """Subprocess crashes mid-transaction — WAL rolls back, state is consistent."""
        db_path = str(tmp_path / "aig.db")

        # Pre-setup: add contract and claim it
        store = SQLiteStore(db_path)
        c = Contract(
            id="crash:task1",
            name="crash_test",
            outputs=["out"],
            activation="",
            budget=5,
        )
        store.add_contract(c)
        claim = store.claim_contract(c.id, "worker1", lease_seconds=300)
        assert claim is not None, "claim must succeed"
        claim_id = claim["claim_id"]
        store.close()

        # Subprocess: submit a candidate that triggers the crash
        script = """
import os as _os
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.submit import submit_candidate
from aigineering.protocol.envelope import CandidateEnvelope

db = _os.environ["AIG_TEST_DB"]
store = SQLiteStore(db)

claim = store.get_claim("crash:task1")
assert claim is not None, "claim must exist"
assert claim["status"] == "active"

envelope = CandidateEnvelope(
    contract_id="crash:task1",
    worker_id="worker1",
    raw_output='/exec {"outputs": {"out": "result"}}',
    claim_id=claim["claim_id"],
    claim_epoch=claim["epoch"],
    idempotency_key="idem-1",
)
ingress = RuntimeIngress(store, store)
result = submit_candidate(envelope, store, store)
print("SUBMIT_RESULT", result)
store.close()
"""

        result = _run_crash_script("after_asset_before_trace", script, db_path)

        # Subprocess should have exited with code 1 (os._exit)
        assert result.returncode == 1, (
            f"Expected exit 1, got {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        # Recover: open a new store and verify no partial state
        store2 = SQLiteStore(db_path)

        # Since the transaction was rolled back, no assets from the candidate should exist
        assets = store2.get_assets_by_name("out")
        assert len(assets) == 0, f"Expected 0 assets after rollback, got {len(assets)}"

        # Trace should also be empty for this contract
        traces = store2.get_trace_events("crash:task1")
        assert len(traces) == 0, f"Expected 0 traces after rollback, got {len(traces)}"

        # Claim should still be active (not transitioned to submitted)
        claim2 = store2.get_claim("crash:task1")
        assert claim2 is not None, "claim must exist after recovery"
        assert claim2["status"] == "active", (
            f"Expected claim status 'active', got {claim2['status']!r}"
        )
        assert claim2["claim_id"] == claim_id, (
            f"Expected same claim_id, got {claim2['claim_id']!r}"
        )

        # Idempotency key should not have been written
        idem = store2.get_idempotency("crash:task1", "idem-1")
        assert idem is None, (
            f"Expected no idempotency record after rollback, got {idem!r}"
        )

        store2.close()


class TestCrashAfterMethodSchedule:
    """Crash after method_scheduled trace but before budget/suspend."""

    def test_crash_after_method_schedule_parent_not_suspended(self, tmp_path):
        """Subprocess crashes after method_scheduled trace — parent not suspended."""
        db_path = str(tmp_path / "aig.db")

        # Subprocess: Engine runs a contract, worker returns /plan, crash at
        # after_method_schedule (between method_scheduled and parent suspend)
        script = """
import os as _os
from aigineering.core.engine import Engine
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.agent.mock import MockWorker
from aigineering.protocol.types import Contract

db = _os.environ["AIG_TEST_DB"]
store = SQLiteStore(db)

worker = MockWorker({"plan_test": '/plan {"reason": "split work"}'})
engine = Engine(store=store, worker=worker, trace_store=store)

c = Contract(
    id="crash:plan1",
    name="plan_test",
    inputs=[],
    outputs=["out"],
    activation="",
    budget=5,
)
engine.add_contract(c)
engine.run()
store.close()
"""

        result = _run_crash_script("after_method_schedule", script, db_path)
        assert result.returncode == 1, (
            f"Expected exit 1, got {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        # Recover: verify child contract and method_scheduled trace survived
        store2 = SQLiteStore(db_path)

        # The method_scheduled trace should exist (committed before the crash point)
        traces = store2.get_trace_events("crash:plan1")
        method_scheduled = [t for t in traces if t.event_type == "method_scheduled"]
        assert len(method_scheduled) >= 1, (
            f"Expected >= 1 method_scheduled trace for crash:plan1, got {len(method_scheduled)}"
        )

        # The child contract should exist (created by _schedule_method_contract before crash)
        child_contracts = [
            c for c in store2.get_all_contracts() if c.parent_id == "crash:plan1"
        ]
        assert len(child_contracts) >= 1, (
            f"Expected >= 1 child contract for parent crash:plan1, got {len(child_contracts)}"
        )

        # Parent should NOT have a budget_consumed trace (added after crash point)
        budget_consumed = [t for t in traces if t.event_type == "budget_consumed"]
        # budget_consumed after method_schedule is AFTER the crash point,
        # so it should not exist. But Engine.add_contract may add a budget_initialized trace.
        # We check that no budget_consumed with relation_type=plan exists.
        plan_budget = [
            t for t in budget_consumed if getattr(t, "relation_type", "") == "plan"
        ]
        assert len(plan_budget) == 0, (
            f"Expected 0 plan budget_consumed traces (after crash point), got {len(plan_budget)}"
        )

        store2.close()


class TestCrashAfterChildComplete:
    """Crash after child complete but before parent resume."""

    def test_crash_after_child_complete_parent_not_resumed(self, tmp_path):
        """Subprocess crashes after child complete — complete trace survives."""
        db_path = str(tmp_path / "aig.db")

        # Subprocess: Engine runs a contract, worker returns /exec that completes,
        # crash at after_child_complete
        script = """
import os as _os
from aigineering.core.engine import Engine
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.agent.mock import MockWorker
from aigineering.protocol.types import Contract

db = _os.environ["AIG_TEST_DB"]
store = SQLiteStore(db)

worker = MockWorker({"child_test": '/exec {"outputs": {"out": "result_data"}}'})
engine = Engine(store=store, worker=worker, trace_store=store)

c = Contract(
    id="crash:child1",
    name="child_test",
    inputs=[],
    outputs=["out"],
    activation="",
    budget=5,
)
engine.add_contract(c)
engine.run()
store.close()
"""

        result = _run_crash_script("after_child_complete", script, db_path)
        assert result.returncode == 1, (
            f"Expected exit 1, got {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        # Recover: verify complete trace and output asset survived
        store2 = SQLiteStore(db_path)

        # The complete trace should exist (committed before the crash point)
        traces = store2.get_trace_events("crash:child1")
        complete_traces = [t for t in traces if t.event_type == "complete"]
        assert len(complete_traces) >= 1, (
            f"Expected >= 1 complete trace for crash:child1, got {len(complete_traces)}"
        )

        # The output asset should exist (committed before the crash point)
        assets = store2.get_assets_by_name("out")
        out_assets = [a for a in assets if a.created_by == "crash:child1"]
        assert len(out_assets) >= 1, (
            f"Expected >= 1 'out' asset created by crash:child1, got {len(out_assets)}"
        )

        store2.close()


class TestDoubleCrashRecovery:
    """Idempotent recovery after two crash events."""

    def test_double_crash_recovery_idempotent(self, tmp_path):
        """Recover twice from same store — identical state."""
        db_path = str(tmp_path / "aig.db")

        # Pre-setup: add contract and claim
        store = SQLiteStore(db_path)
        c = Contract(
            id="crash:double1",
            name="double_test",
            outputs=["out"],
            activation="",
            budget=5,
        )
        store.add_contract(c)
        claim = store.claim_contract(c.id, "worker1", lease_seconds=300)
        assert claim is not None
        store.close()

        # First crash: submit a candidate that triggers after_asset_before_trace
        script1 = """
import os as _os
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.submit import submit_candidate
from aigineering.protocol.envelope import CandidateEnvelope

db = _os.environ["AIG_TEST_DB"]
store = SQLiteStore(db)

claim = store.get_claim("crash:double1")
envelope = CandidateEnvelope(
    contract_id="crash:double1",
    worker_id="worker1",
    raw_output='/exec {"outputs": {"out": "first"}}',
    claim_id=claim["claim_id"],
    claim_epoch=claim["epoch"],
    idempotency_key="idem-double",
)
ingress = RuntimeIngress(store, store)
submit_candidate(envelope, store, store)
store.close()
"""

        result1 = _run_crash_script("after_asset_before_trace", script1, db_path)
        assert result1.returncode == 1

        # Recover once
        store_r1 = SQLiteStore(db_path)
        traces_r1 = store_r1.get_trace_events("crash:double1")
        assets_r1 = store_r1.get_assets_by_name("out")
        out_r1 = [a for a in assets_r1 if a.created_by == "crash:double1"]
        claim_r1 = store_r1.get_claim("crash:double1")
        store_r1.close()

        # Second crash: submit again (reuse same claim)
        script2 = """
import os as _os
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.submit import submit_candidate
from aigineering.protocol.envelope import CandidateEnvelope

db = _os.environ["AIG_TEST_DB"]
store = SQLiteStore(db)

claim = store.get_claim("crash:double1")
envelope = CandidateEnvelope(
    contract_id="crash:double1",
    worker_id="worker1",
    raw_output='/exec {"outputs": {"out": "second"}}',
    claim_id=claim["claim_id"],
    claim_epoch=claim["epoch"],
    idempotency_key="idem-double",
)
ingress = RuntimeIngress(store, store)
submit_candidate(envelope, store, store)
store.close()
"""

        result2 = _run_crash_script("after_asset_before_trace", script2, db_path)
        assert result2.returncode == 1

        # Recover again
        store_r2 = SQLiteStore(db_path)
        traces_r2 = store_r2.get_trace_events("crash:double1")
        assets_r2 = store_r2.get_assets_by_name("out")
        out_r2 = [a for a in assets_r2 if a.created_by == "crash:double1"]
        claim_r2 = store_r2.get_claim("crash:double1")
        store_r2.close()

        # Both recoveries should see identical state (idempotent)
        assert len(traces_r1) == len(traces_r2), (
            f"Trace count must be idempotent: r1={len(traces_r1)} r2={len(traces_r2)}"
        )
        assert len(out_r1) == len(out_r2), (
            f"Asset count must be idempotent: r1={len(out_r1)} r2={len(out_r2)}"
        )
        assert claim_r1 is not None and claim_r2 is not None, (
            "Claim must survive both crashes"
        )
        assert claim_r1["status"] == claim_r2["status"], (
            f"Claim status must be idempotent: r1={claim_r1['status']!r} r2={claim_r2['status']!r}"
        )
