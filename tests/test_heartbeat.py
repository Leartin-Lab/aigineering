"""Tests for Heartbeat presence renewal (v0.3.16).

Heartbeat is presence/renewal ONLY. It MUST NOT dispatch work assignments.
"""

from datetime import datetime, timezone

import pytest

from aigineering.core.claims import ClaimStore
from aigineering.core.heartbeat import Heartbeat, HeartbeatStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def claim_store() -> ClaimStore:
    return ClaimStore()


@pytest.fixture
def hb_store(claim_store: ClaimStore) -> HeartbeatStore:
    return HeartbeatStore(claim_store)


# ---------------------------------------------------------------------------
# heartbeat() — presence recording
# ---------------------------------------------------------------------------


def test_heartbeat_records_presence(hb_store: HeartbeatStore):
    """heartbeat() records worker presence with metadata."""
    hb = hb_store.heartbeat(
        worker_id="worker-A",
        capabilities=("tool_search", "tool_read"),
    )
    assert hb.worker_id == "worker-A"
    assert hb.capabilities == ("tool_search", "tool_read")
    assert hb.last_seen > datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()
    assert hb.active_claim_ids == ()
    assert hb.renewal_deadline > hb.last_seen


def test_heartbeat_default_capabilities(hb_store: HeartbeatStore):
    """heartbeat() with no capabilities defaults to empty tuple."""
    hb = hb_store.heartbeat(worker_id="worker-A")
    assert hb.capabilities == ()


def test_heartbeat_default_claim_ids(hb_store: HeartbeatStore):
    """heartbeat() with no claim_ids defaults to empty tuple."""
    hb = hb_store.heartbeat(worker_id="worker-A")
    assert hb.active_claim_ids == ()


# ---------------------------------------------------------------------------
# heartbeat() — lease extension
# ---------------------------------------------------------------------------


def test_heartbeat_extends_lease_on_active_claims(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """heartbeat() extends the lease_until on each active claim."""
    c = claim_store.claim("contract-1", "worker-A", lease_seconds=10)
    assert c is not None
    original_lease = c.lease_until

    hb_store.heartbeat(
        worker_id="worker-A",
        claim_ids=(c.claim_id,),
    )

    updated = claim_store.get_claim(c.claim_id)
    assert updated is not None
    assert updated.lease_until > original_lease


def test_heartbeat_extends_multiple_leases(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """heartbeat() extends leases for all provided claim IDs."""
    c1 = claim_store.claim("contract-1", "worker-A", lease_seconds=10)
    c2 = claim_store.claim("contract-2", "worker-A", lease_seconds=10)
    assert c1 is not None
    assert c2 is not None

    original1 = c1.lease_until
    original2 = c2.lease_until

    hb_store.heartbeat(
        worker_id="worker-A",
        claim_ids=(c1.claim_id, c2.claim_id),
    )

    u1 = claim_store.get_claim(c1.claim_id)
    u2 = claim_store.get_claim(c2.claim_id)
    assert u1 is not None and u1.lease_until > original1
    assert u2 is not None and u2.lease_until > original2


def test_heartbeat_stores_active_claim_ids(
    hb_store: HeartbeatStore, claim_store: ClaimStore
):
    """heartbeat() stores the provided claim_ids in the heartbeat record."""
    c = claim_store.claim("contract-1", "worker-A")
    assert c is not None

    hb = hb_store.heartbeat(worker_id="worker-A", claim_ids=(c.claim_id,))
    assert hb.active_claim_ids == (c.claim_id,)


# ---------------------------------------------------------------------------
# renew_lease()
# ---------------------------------------------------------------------------


def test_renew_lease_extends_active_claim(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """renew_lease() extends an active claim's lease."""
    c = claim_store.claim("contract-1", "worker-A", lease_seconds=10)
    assert c is not None
    original = c.lease_until

    ok = hb_store.renew_lease(c.claim_id, extend_seconds=60)
    assert ok is True

    updated = claim_store.get_claim(c.claim_id)
    assert updated is not None
    assert updated.lease_until > original


def test_renew_lease_non_active_returns_false(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """renew_lease() returns False for non-active claims."""
    c = claim_store.claim("contract-1", "worker-A")
    claim_store.release_claim(c.claim_id)

    assert hb_store.renew_lease(c.claim_id) is False


def test_renew_lease_nonexistent_returns_false(hb_store: HeartbeatStore):
    """renew_lease() returns False for unknown claim IDs."""
    assert hb_store.renew_lease("nonexistent") is False


# ---------------------------------------------------------------------------
# get_heartbeat()
# ---------------------------------------------------------------------------


def test_get_heartbeat_returns_record(hb_store: HeartbeatStore):
    """get_heartbeat() returns the last heartbeat for a worker."""
    hb_store.heartbeat(worker_id="worker-A")
    hb = hb_store.get_heartbeat("worker-A")
    assert hb is not None
    assert hb.worker_id == "worker-A"


def test_get_heartbeat_unknown_returns_none(hb_store: HeartbeatStore):
    """get_heartbeat() returns None for unknown workers."""
    assert hb_store.get_heartbeat("unknown") is None


def test_get_heartbeat_returns_latest(hb_store: HeartbeatStore):
    """get_heartbeat() always returns the most recent heartbeat."""
    hb_store.heartbeat(worker_id="worker-A", capabilities=("v1",))
    hb_store.heartbeat(worker_id="worker-A", capabilities=("v2",))

    hb = hb_store.get_heartbeat("worker-A")
    assert hb is not None
    assert hb.capabilities == ("v2",)


# ---------------------------------------------------------------------------
# NO work dispatch
# ---------------------------------------------------------------------------


def test_heartbeat_does_not_create_claims(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """heartbeat() MUST NOT create new claims or dispatch work.

    Presence renewal is observational only.
    """
    # Record initial state
    initial_claims = len(claim_store.check_expired())

    hb_store.heartbeat(
        worker_id="worker-A",
        capabilities=("tool_search",),
        claim_ids=("nonexistent-claim",),
    )

    # heartbeat() should not have created any claims
    assert claim_store.get_claim("nonexistent-claim") is None
    # No claims should have been added
    assert len(claim_store.check_expired()) == initial_claims


def test_heartbeat_does_not_assign_contracts(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """heartbeat() MUST NOT assign contracts to workers.

    Claiming contracts is the responsibility of the engine/planner,
    not the heartbeat layer.
    """
    # Create a claim for worker-A
    c = claim_store.claim("contract-1", "worker-A")
    assert c is not None

    # worker-B sends heartbeat — should NOT get any claim assigned
    hb_store.heartbeat(worker_id="worker-B")
    hb = hb_store.get_heartbeat("worker-B")
    assert hb is not None
    assert hb.active_claim_ids == ()  # no claims assigned

    # contract-1 should still be claimed by worker-A only
    # (worker-B cannot claim it because it's already active)
    c2 = claim_store.claim("contract-1", "worker-B")
    assert c2 is None  # duplicate prevention still holds


def test_heartbeat_no_work_dispatch_on_renewal(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """renew_lease() only extends — it does not change claim ownership."""
    c = claim_store.claim("contract-1", "worker-A")
    assert c is not None

    hb_store.renew_lease(c.claim_id, extend_seconds=60)

    updated = claim_store.get_claim(c.claim_id)
    assert updated is not None
    assert updated.worker_id == "worker-A"  # ownership unchanged
    assert updated.contract_id == "contract-1"  # contract unchanged
    assert updated.status == "active"  # status unchanged


# ---------------------------------------------------------------------------
# No heartbeat → expiry
# ---------------------------------------------------------------------------


def test_no_heartbeat_claim_expires(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """Without heartbeat renewal, claims expire normally."""
    c = claim_store.claim("contract-1", "worker-A", lease_seconds=-1)
    assert c is not None

    # No heartbeat sent — claim should expire
    expired = claim_store.check_expired()
    assert len(expired) == 1
    assert expired[0].claim_id == c.claim_id
    assert expired[0].status == "expired"


def test_heartbeat_prevents_expiry(
    claim_store: ClaimStore,
    hb_store: HeartbeatStore,
):
    """Regular heartbeats extend the lease, preventing expiry."""
    # Create a claim with very short lease (1 second)
    c = claim_store.claim("contract-1", "worker-A", lease_seconds=1)
    assert c is not None

    # Send heartbeat immediately to extend the lease
    hb_store.heartbeat(worker_id="worker-A", claim_ids=(c.claim_id,))

    # Lease should have been extended beyond the original 1 second
    updated = claim_store.get_claim(c.claim_id)
    assert updated is not None
    assert updated.lease_until > c.lease_until


# ---------------------------------------------------------------------------
# Heartbeat dataclass
# ---------------------------------------------------------------------------


def test_heartbeat_fields_are_tuples():
    """Heartbeat capabilities and active_claim_ids are stored as tuples."""
    hb = Heartbeat(
        worker_id="w1",
        capabilities=["search"],  # type: ignore[arg-type]
        last_seen="2025-01-01T00:00:00+00:00",
        active_claim_ids=["c1"],  # type: ignore[arg-type]
        renewal_deadline="2025-01-01T00:00:30+00:00",
    )
    assert isinstance(hb.capabilities, tuple)
    assert isinstance(hb.active_claim_ids, tuple)
