"""Claim/lease model for contract-to-worker binding (v0.3.15).

A Claim represents an exclusive lease on a contract by a worker.
Only one active claim per contract is allowed at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from aigineering.core.ids import compute_content_hash, now_iso


def _lease_id(contract_id: str, worker_id: str, leased_at: str) -> str:
    """Deterministic claim identity with ``lease:`` domain tag."""
    raw = f"{contract_id}|{worker_id}|{leased_at}"
    return f"lease:{compute_content_hash(raw)}"


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO timestamp to a timezone-aware datetime."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_expired(lease_until: str) -> bool:
    """Return True if *lease_until* is in the past."""
    return _parse_iso(lease_until) < datetime.now(timezone.utc)


@dataclass(frozen=True)
class Claim:
    """An exclusive lease on a contract held by a worker.

    States: active → submitted | released | expired
    """

    claim_id: str
    contract_id: str
    worker_id: str
    lease_until: str  # ISO 8601 timestamp
    status: str  # "active" | "submitted" | "released" | "expired"


class ClaimStore:
    """In-memory store for contract claims with duplicate prevention.

    Guarantees:
      - At most one active claim per contract.
      - Stale submit is rejected (lease expiry checked on submit).
      - Expired claims are discoverable via *check_expired*.
    """

    def __init__(self) -> None:
        self._claims: dict[str, Claim] = {}
        # contract_id → claim_id for currently active claims
        self._active_contracts: dict[str, str] = {}
        self._claimed_contracts: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def claim(
        self,
        contract_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Claim | None:
        """Acquire an exclusive claim on *contract_id*.

        Returns *None* when the contract has ever been claimed.  Recovery or
        retry must create a new contract instead of returning the same contract
        to an unclaimed state.
        """
        if contract_id in self._claimed_contracts:
            return None

        leased_at = now_iso()
        lease_until_dt = datetime.now(timezone.utc).timestamp() + lease_seconds
        lease_until = datetime.fromtimestamp(
            lease_until_dt, tz=timezone.utc
        ).isoformat()

        claim = Claim(
            claim_id=_lease_id(contract_id, worker_id, leased_at),
            contract_id=contract_id,
            worker_id=worker_id,
            lease_until=lease_until,
            status="active",
        )
        self._claims[claim.claim_id] = claim
        self._active_contracts[contract_id] = claim.claim_id
        self._claimed_contracts.add(contract_id)
        return claim

    def submit_claim(self, claim_id: str) -> bool:
        """Transition an active claim to submitted.

        Rejects (returns ``False``) when:
          - The claim does not exist.
          - The claim is not active.
          - The lease has expired (stale submit handling).

        Returns ``True`` on successful transition.
        """
        claim = self._claims.get(claim_id)
        if claim is None or claim.status != "active":
            return False

        if _is_expired(claim.lease_until):
            # Stale submit — expire the claim
            expired = replace(claim, status="expired")
            self._claims[claim_id] = expired
            self._active_contracts.pop(claim.contract_id, None)
            return False

        submitted = replace(claim, status="submitted")
        self._claims[claim_id] = submitted
        del self._active_contracts[claim.contract_id]
        return True

    def release_claim(self, claim_id: str) -> None:
        """Release an active claim without making the contract claimable again.

        Only transitions from *active* (idempotent for non-active claims).
        """
        claim = self._claims.get(claim_id)
        if claim is None or claim.status != "active":
            return

        released = replace(claim, status="released")
        self._claims[claim_id] = released
        self._active_contracts.pop(claim.contract_id, None)

    def check_expired(self) -> list[Claim]:
        """Find and expire all active claims whose lease has elapsed.

        Returns the list of newly-expired claims (may be empty).
        """
        expired_claims: list[Claim] = []
        now = datetime.now(timezone.utc)

        for contract_id, claim_id in list(self._active_contracts.items()):
            claim = self._claims.get(claim_id)
            if claim is None:
                # Clean up stale index entry
                del self._active_contracts[contract_id]
                continue
            if claim.status == "active" and _parse_iso(claim.lease_until) < now:
                expired = replace(claim, status="expired")
                self._claims[claim_id] = expired
                del self._active_contracts[claim.contract_id]
                expired_claims.append(expired)

        return expired_claims

    def get_claim(self, claim_id: str) -> Claim | None:
        """Return the claim for *claim_id*, or *None*."""
        return self._claims.get(claim_id)

    def extend_lease(self, claim_id: str, extend_seconds: int = 30) -> bool:
        """Extend the lease of an active claim by *extend_seconds*.

        Returns ``True`` if the lease was extended, ``False`` if the claim
        is not active or does not exist.
        """
        claim = self._claims.get(claim_id)
        if claim is None or claim.status != "active":
            return False

        new_until_dt = _parse_iso(claim.lease_until).timestamp() + extend_seconds
        new_lease_until = datetime.fromtimestamp(
            new_until_dt, tz=timezone.utc
        ).isoformat()
        self._claims[claim_id] = replace(claim, lease_until=new_lease_until)
        return True
