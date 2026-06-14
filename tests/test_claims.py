"""Tests for Claim/lease model (v0.3.15)."""

from datetime import datetime, timezone

import pytest

from aigineering.core.claims import Claim, ClaimStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> ClaimStore:
    return ClaimStore()


# ---------------------------------------------------------------------------
# claim()
# ---------------------------------------------------------------------------


def test_claim_creates_active_claim(store: ClaimStore):
    """claim() creates an active claim with correct metadata."""
    c = store.claim("contract-1", "worker-A", lease_seconds=60)
    assert c is not None
    assert c.contract_id == "contract-1"
    assert c.worker_id == "worker-A"
    assert c.status == "active"
    assert c.claim_id.startswith("lease:")
    # lease_until should be ~60 s in the future
    assert c.lease_until > datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()


def test_claim_duplicate_rejected(store: ClaimStore):
    """claim() returns None when contract already has an active claim."""
    c1 = store.claim("contract-1", "worker-A")
    assert c1 is not None

    c2 = store.claim("contract-1", "worker-B")
    assert c2 is None  # duplicate prevention


def test_claim_different_contracts_allowed(store: ClaimStore):
    """Different contracts can be claimed simultaneously."""
    c1 = store.claim("contract-1", "worker-A")
    c2 = store.claim("contract-2", "worker-A")
    assert c1 is not None
    assert c2 is not None
    assert c1.contract_id != c2.contract_id


def test_claim_different_workers_different_contracts(store: ClaimStore):
    """Different workers can claim different contracts."""
    c1 = store.claim("contract-1", "worker-A")
    c2 = store.claim("contract-2", "worker-B")
    assert c1 is not None
    assert c2 is not None


# ---------------------------------------------------------------------------
# submit_claim()
# ---------------------------------------------------------------------------


def test_submit_claim_transitions_to_submitted(store: ClaimStore):
    """submit_claim() transitions active → submitted."""
    c = store.claim("contract-1", "worker-A")
    assert c is not None

    ok = store.submit_claim(c.claim_id)
    assert ok is True

    retrieved = store.get_claim(c.claim_id)
    assert retrieved is not None
    assert retrieved.status == "submitted"


def test_submit_claim_frees_contract_for_reclaim(store: ClaimStore):
    """After submit, the contract can be claimed again."""
    c1 = store.claim("contract-1", "worker-A")
    store.submit_claim(c1.claim_id)

    c2 = store.claim("contract-1", "worker-B")
    assert c2 is not None
    assert c2.contract_id == "contract-1"
    assert c2.worker_id == "worker-B"


def test_submit_claim_nonexistent_returns_false(store: ClaimStore):
    """submit_claim() returns False for unknown claim_id."""
    assert store.submit_claim("nonexistent") is False


def test_submit_claim_non_active_returns_false(store: ClaimStore):
    """submit_claim() returns False for non-active claims."""
    c = store.claim("contract-1", "worker-A")
    store.release_claim(c.claim_id)

    assert store.submit_claim(c.claim_id) is False


def test_submit_claim_already_submitted_returns_false(store: ClaimStore):
    """submit_claim() is idempotent-reject — second submit fails."""
    c = store.claim("contract-1", "worker-A")
    assert store.submit_claim(c.claim_id) is True
    assert store.submit_claim(c.claim_id) is False


# ---------------------------------------------------------------------------
# Stale submit handling
# ---------------------------------------------------------------------------


def test_submit_claim_stale_lease_rejected(store: ClaimStore):
    """submit_claim() rejects claims whose lease has expired."""
    c = store.claim("contract-1", "worker-A", lease_seconds=-1)
    assert c is not None
    assert c.status == "active"

    ok = store.submit_claim(c.claim_id)
    assert ok is False

    retrieved = store.get_claim(c.claim_id)
    assert retrieved is not None
    assert retrieved.status == "expired"


# ---------------------------------------------------------------------------
# release_claim()
# ---------------------------------------------------------------------------


def test_release_claim_transitions_to_released(store: ClaimStore):
    """release_claim() transitions active → released."""
    c = store.claim("contract-1", "worker-A")
    store.release_claim(c.claim_id)

    retrieved = store.get_claim(c.claim_id)
    assert retrieved is not None
    assert retrieved.status == "released"


def test_release_claim_frees_contract(store: ClaimStore):
    """After release, the contract can be claimed again."""
    c1 = store.claim("contract-1", "worker-A")
    store.release_claim(c1.claim_id)

    c2 = store.claim("contract-1", "worker-B")
    assert c2 is not None


def test_release_claim_nonexistent_no_error(store: ClaimStore):
    """release_claim() on non-existent claim is a no-op."""
    store.release_claim("nonexistent")  # should not raise


def test_release_claim_non_active_no_op(store: ClaimStore):
    """release_claim() on already-submitted claim is a no-op."""
    c = store.claim("contract-1", "worker-A")
    store.submit_claim(c.claim_id)
    store.release_claim(c.claim_id)  # no-op, stays submitted

    retrieved = store.get_claim(c.claim_id)
    assert retrieved is not None
    assert retrieved.status == "submitted"


# ---------------------------------------------------------------------------
# check_expired()
# ---------------------------------------------------------------------------


def test_check_expired_transitions_expired_claims(store: ClaimStore):
    """check_expired() finds and expires claims past their lease."""
    c = store.claim("contract-1", "worker-A", lease_seconds=-1)
    assert c is not None
    assert c.status == "active"

    expired = store.check_expired()
    assert len(expired) == 1
    assert expired[0].claim_id == c.claim_id
    assert expired[0].status == "expired"

    retrieved = store.get_claim(c.claim_id)
    assert retrieved is not None
    assert retrieved.status == "expired"


def test_check_expired_returns_empty_when_none_expired(store: ClaimStore):
    """check_expired() returns empty list when all leases are valid."""
    store.claim("contract-1", "worker-A", lease_seconds=3600)
    expired = store.check_expired()
    assert expired == []


def test_check_expired_frees_contract(store: ClaimStore):
    """After expiry, the contract can be claimed again."""
    store.claim("contract-1", "worker-A", lease_seconds=-1)
    store.check_expired()

    c2 = store.claim("contract-1", "worker-B")
    assert c2 is not None


def test_check_expired_only_expires_active(store: ClaimStore):
    """check_expired() only affects active claims."""
    c = store.claim("contract-1", "worker-A", lease_seconds=3600)
    store.release_claim(c.claim_id)

    expired = store.check_expired()
    assert expired == []

    retrieved = store.get_claim(c.claim_id)
    assert retrieved is not None
    assert retrieved.status == "released"


# ---------------------------------------------------------------------------
# get_claim()
# ---------------------------------------------------------------------------


def test_get_claim_returns_claim(store: ClaimStore):
    """get_claim() returns the claim by ID."""
    c = store.claim("contract-1", "worker-A")
    retrieved = store.get_claim(c.claim_id)
    assert retrieved == c


def test_get_claim_nonexistent_returns_none(store: ClaimStore):
    """get_claim() returns None for unknown IDs."""
    assert store.get_claim("nonexistent") is None


# ---------------------------------------------------------------------------
# extend_lease()
# ---------------------------------------------------------------------------


def test_extend_lease_prolongs_active_claim(store: ClaimStore):
    """extend_lease() pushes lease_until forward on active claims."""
    c = store.claim("contract-1", "worker-A", lease_seconds=10)
    assert c is not None
    original = c.lease_until

    ok = store.extend_lease(c.claim_id, extend_seconds=60)
    assert ok is True

    updated = store.get_claim(c.claim_id)
    assert updated is not None
    assert updated.lease_until > original


def test_extend_lease_non_active_returns_false(store: ClaimStore):
    """extend_lease() returns False for non-active claims."""
    c = store.claim("contract-1", "worker-A")
    store.release_claim(c.claim_id)

    assert store.extend_lease(c.claim_id) is False


def test_extend_lease_nonexistent_returns_false(store: ClaimStore):
    """extend_lease() returns False for unknown claim IDs."""
    assert store.extend_lease("nonexistent") is False


# ---------------------------------------------------------------------------
# Claim dataclass immutability
# ---------------------------------------------------------------------------


def test_claim_is_frozen():
    """Claim dataclass is frozen (immutable)."""
    c = Claim(
        claim_id="lease:abc",
        contract_id="c1",
        worker_id="w1",
        lease_until="2025-01-01T00:00:00+00:00",
        status="active",
    )
    with pytest.raises(Exception):
        c.status = "submitted"  # type: ignore[misc]
