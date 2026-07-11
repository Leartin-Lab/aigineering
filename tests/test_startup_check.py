"""Tests for lease-based runtime lifecycle startup checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aigineering.cli.task_state import project_task_status
from aigineering.core.ids import hash_contract_v2
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.startup_check import begin_runtime_startup, end_runtime
from aigineering.protocol.types import Contract


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _ready_contract(name: str = "stale_ready") -> Contract:
    return Contract(
        id=hash_contract_v2(
            name=name,
            description="",
            inputs=[],
            outputs=["report"],
            activation="",
            budget=3,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name=name,
        outputs=("report",),
        activation="",
        budget=3,
    )


def _old_heartbeat(seconds_ago: int = 120) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _add_expired_claim(store, contract_id: str, worker_id: str) -> None:
    """Persist an active claim with an expired lease (120 s ago)."""
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    store.persist_claim(
        claim_id=f"claim:expired:{contract_id}",
        contract_id=contract_id,
        worker_id=worker_id,
        lease_until=past_time,
        status="active",
    )


# ------------------------------------------------------------------
# existing behaviour adapted to lease model
# ------------------------------------------------------------------


def test_clean_startup_no_orphans_does_not_mark_recovery():
    """No orphaned runtimes → nothing is marked recovery_required."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    contract = ingress.accept_contract(_ready_contract())

    result = begin_runtime_startup(store, worker_id="test-worker")
    end_runtime(store, result.runtime_owner, worker_id="test-worker")

    assert result.orphaned_runtime_ids == ()
    assert result.recovery_required_contract_ids == ()
    assert project_task_status(contract, store)["status"] == "ready"


def test_startup_markers_are_append_only():
    """Multiple clean starts/stops append distinct entries."""
    store = SQLiteStore(":memory:")

    first = begin_runtime_startup(store, worker_id="test-worker")
    end_runtime(store, first.runtime_owner, worker_id="test-worker")
    second = begin_runtime_startup(store, worker_id="test-worker")
    end_runtime(store, second.runtime_owner, worker_id="test-worker")

    entries = store.get_by_contract("runtime_startup")
    started = [e for e in entries if e.event_type == "runtime_started"]
    stopped = [e for e in entries if e.event_type == "runtime_stopped"]
    assert len(started) >= 2
    assert len(stopped) >= 2
    assert first.runtime_owner != second.runtime_owner


def test_startup_preserves_explicit_target_task():
    """Preserved contracts are NOT marked recovery_required."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    target = ingress.accept_contract(_ready_contract("target_task"))
    stale = ingress.accept_contract(_ready_contract("other_ready"))

    # Simulate an orphaned runtime whose lease expired.
    store.upsert_runtime_lifecycle("dead-runtime", _old_heartbeat(120), "active")
    # stale has an expired claim — it was being worked on by the dead runtime.
    _add_expired_claim(store, stale.id, "dead-runtime")
    # target has NO claim — it's preserved anyway, but even without preservation,
    # unclaimed contracts are not attributed to any runtime.

    result = begin_runtime_startup(
        store,
        preserve_contract_ids={target.id},
        worker_id="test-worker",
    )
    end_runtime(store, result.runtime_owner, worker_id="test-worker")

    assert len(result.orphaned_runtime_ids) >= 1
    assert target.id not in result.recovery_required_contract_ids
    assert stale.id in result.recovery_required_contract_ids
    assert project_task_status(target, store)["status"] == "ready"


# ------------------------------------------------------------------
# new lease-based tests
# ------------------------------------------------------------------


def test_active_lease_prevents_interference():
    """Runtime B sees runtime A's active lease → skips A's tasks."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    contract = ingress.accept_contract(_ready_contract())

    # Simulate an active (non-expired) runtime A.
    store.upsert_runtime_lifecycle(
        "runtime-A", _old_heartbeat(seconds_ago=10), "active"
    )

    # Runtime B starts.  A's heartbeat is only 10 s old → not orphaned
    # at the default TTL of 60 s.
    result = begin_runtime_startup(store, worker_id="test-worker-B")
    end_runtime(store, result.runtime_owner, worker_id="test-worker-B")

    assert result.orphaned_runtime_ids == ()
    assert result.recovery_required_contract_ids == ()
    assert project_task_status(contract, store)["status"] == "ready"


def test_lease_expiry_marks_recovery_required():
    """Runtime A crashed (lease expired) → B marks contracts with expired claims."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    contract = ingress.accept_contract(_ready_contract())

    # Simulate a dead runtime whose lease expired 120 s ago.
    store.upsert_runtime_lifecycle("dead-runtime", _old_heartbeat(120), "active")
    # Create an expired claim — dead-runtime was actively working on this contract.
    _add_expired_claim(store, contract.id, "dead-runtime")

    # Runtime B starts with default TTL of 60 s → A is orphaned.
    result = begin_runtime_startup(store, worker_id="test-worker-B")
    end_runtime(store, result.runtime_owner, worker_id="test-worker-B")

    assert "dead-runtime" in result.orphaned_runtime_ids
    assert contract.id in result.recovery_required_contract_ids

    # Verify the trace has a recovery_required entry.
    entries = store.get_by_contract(contract.id)
    recovery_events = [e for e in entries if e.event_type == "recovery_required"]
    assert len(recovery_events) >= 1
    assert any(
        "orphaned runtime" in frag.lower()
        for e in recovery_events
        for frag in e.rejected_fragments
    )


def test_recovery_required_not_cancelled():
    """Recovery_required tasks are NOT cancelled (not terminal)."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    contract = ingress.accept_contract(_ready_contract())

    store.upsert_runtime_lifecycle("dead-runtime", _old_heartbeat(120), "active")
    _add_expired_claim(store, contract.id, "dead-runtime")

    result = begin_runtime_startup(store, worker_id="test-worker-B")
    end_runtime(store, result.runtime_owner, worker_id="test-worker-B")

    assert contract.id in result.recovery_required_contract_ids

    # No cancelled event.
    entries = store.get_by_contract(contract.id)
    assert not any(e.event_type == "cancelled" for e in entries)

    # Status is NOT cancelled — it's still ready (or blocked).
    status = project_task_status(contract, store)
    assert status["status"] != "cancelled"
    assert status["terminal"] is False


def test_runtime_owner_in_lifecycle():
    """runtime_owner UUID is stored in the runtime_lifecycle table."""
    store = SQLiteStore(":memory:")

    result = begin_runtime_startup(store, worker_id="test-worker")
    lifecycle = store.get_runtime_lifecycle(result.runtime_owner)

    assert lifecycle is not None
    assert lifecycle["runtime_id"] == result.runtime_owner
    assert lifecycle["state"] == "active"
    assert lifecycle["started_at"] is not None

    # After end_runtime the state should be 'stopped'.
    end_runtime(store, result.runtime_owner, worker_id="test-worker")
    lifecycle_after = store.get_runtime_lifecycle(result.runtime_owner)
    assert lifecycle_after["state"] == "stopped"
    assert lifecycle_after["stopped_at"] is not None


def test_multiple_orphaned_runtimes_all_marked():
    """All contracts with expired claims across multiple orphaned runtimes are marked."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    c1 = ingress.accept_contract(_ready_contract("task_1"))
    c2 = ingress.accept_contract(_ready_contract("task_2"))

    store.upsert_runtime_lifecycle("dead-1", _old_heartbeat(120), "active")
    store.upsert_runtime_lifecycle("dead-2", _old_heartbeat(90), "active")
    _add_expired_claim(store, c1.id, "dead-1")
    _add_expired_claim(store, c2.id, "dead-2")

    result = begin_runtime_startup(
        store, worker_id="test-worker-B", lease_ttl_seconds=60
    )
    end_runtime(store, result.runtime_owner, worker_id="test-worker-B")

    assert len(result.orphaned_runtime_ids) >= 2
    assert set(result.recovery_required_contract_ids) == {c1.id, c2.id}


def test_stopped_runtime_not_orphaned():
    """A runtime with state='stopped' is never considered orphaned."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    contract = ingress.accept_contract(_ready_contract())

    # Runtime that exited cleanly (stopped state, old heartbeat).
    store.upsert_runtime_lifecycle("clean-runtime", _old_heartbeat(120), "stopped")

    result = begin_runtime_startup(store, worker_id="test-worker-B")
    end_runtime(store, result.runtime_owner, worker_id="test-worker-B")

    # Stopped runtime is not orphaned → no recovery marking.
    assert result.orphaned_runtime_ids == ()
    assert result.recovery_required_contract_ids == ()
    assert project_task_status(contract, store)["status"] == "ready"


def test_unclaimed_contracts_not_orphaned():
    """Unclaimed contracts without claims are NOT marked recovery_required."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    unclaimed = ingress.accept_contract(_ready_contract("unclaimed_ready"))

    # Runtime that crashed (orphaned) but never claimed this contract.
    store.upsert_runtime_lifecycle("dead-runtime", _old_heartbeat(120), "active")

    result = begin_runtime_startup(store, worker_id="test-worker-B")
    end_runtime(store, result.runtime_owner, worker_id="test-worker-B")

    assert "dead-runtime" in result.orphaned_runtime_ids
    # Unclaimed contract is NOT attributed to any runtime.
    assert unclaimed.id not in result.recovery_required_contract_ids
    assert project_task_status(unclaimed, store)["status"] == "ready"
