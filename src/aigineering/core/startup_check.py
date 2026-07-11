"""Runtime startup self-checks using lease-based lifecycle semantics.

The check is intentionally outside ``Engine``.  Engine should execute the
enabled contracts it is given; startup hygiene is a control-plane concern over
durable runtime records.

Key differences from the trace-based approach (pre-v0.5):
- Uses ``runtime_lifecycle`` table with heartbeat leases instead of scanning
  ``runtime_started`` / ``runtime_stopped`` trace entries.
- No PID checks.  Runtime identity is a UUID instance-id.
- Orphaned (lease-expired) runtimes cause **recovery_required** marking,
  never auto-cancellation.  Cancellation is a separate explicit decision.
- Active leases prevent interference — a live runtime's tasks are untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from aigineering.core.ids import now_iso
from aigineering.core.trace import create_entry
from aigineering.protocol.types import TraceEntry


_TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"complete", "failed", "cancelled", "unreachable"}
)
_RUNTIME_STARTED: str = "runtime_started"
_RUNTIME_STOPPED: str = "runtime_stopped"
_RECOVERY_REQUIRED: str = "recovery_required"

# Defaults for lease-based lifecycle.
_DEFAULT_LEASE_TTL_SECONDS: int = 60
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS: int = 15


@dataclass(frozen=True)
class StartupCheckResult:
    """Result of a runtime startup self-check.

    ``runtime_owner`` is the UUID instance-id used in the ``runtime_lifecycle``
    table.  ``runtime_id`` is the legacy trace-target string for backward
    compatibility.
    """

    runtime_id: str
    runtime_owner: str
    orphaned_runtime_ids: tuple[str, ...]
    recovery_required_contract_ids: tuple[str, ...]


def begin_runtime_startup(
    store,
    *,
    preserve_contract_ids: set[str] | None = None,
    worker_id: str = "runtime",
    lease_ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS,
) -> StartupCheckResult:
    """Run startup hygiene using lease-based runtime lifecycle.

    1. Generate a UUID ``runtime_owner`` for this runtime instance.
    2. Query ``store.get_orphaned_runtimes(lease_ttl_seconds)`` to find
       runtimes whose heartbeat lease expired.
    3. For **each** orphaned runtime, scan all contracts for expired
       worker claims and append a ``recovery_required`` trace entry —
       **without** cancelling the contract.
    4. Record a ``runtime_started`` trace event keyed on ``runtime_owner``.
    5. Upsert the ``runtime_lifecycle`` row with ``state='active'``.

    Explicitly preserved contracts (``preserve_contract_ids``) are never
    marked recovery-required.  Unclaimed contracts (no claim at all) are
    left as ready — they are not attributable to any runtime.
    """
    preserve = preserve_contract_ids or set()
    runtime_owner = str(uuid4())
    runtime_id = f"runtime:{runtime_owner}"

    # ── orphan detection via SQLite runtime_lifecycle table ──────────────
    get_orphaned = getattr(store, "get_orphaned_runtimes", None)
    orphaned: list[dict] = list(get_orphaned(lease_ttl_seconds)) if get_orphaned else []
    orphaned_ids: tuple[str, ...] = tuple(o["runtime_id"] for o in orphaned)

    recovery_required: list[str] = []

    if orphaned:
        for contract in store.get_all_contracts():
            if contract.id in preserve:
                continue
            if _has_expired_claim(contract, store):
                _append(
                    store,
                    _make_entry(
                        store,
                        contract_id=contract.id,
                        event_type=_RECOVERY_REQUIRED,
                        worker_id=worker_id,
                        relation_type="startup_self_check",
                        relation_target=runtime_owner,
                        rejected_fragments=[
                            "[recovery_required] startup_self_check: "
                            f"orphaned runtime(s) {orphaned_ids}; "
                            "expired claim marked for recovery"
                        ],
                    ),
                )
                recovery_required.append(contract.id)

    # ── trace: startup self-check summary ───────────────────────────────
    _append(
        store,
        _make_entry(
            store,
            contract_id="runtime_startup",
            event_type="runtime_self_check",
            worker_id=worker_id,
            relation_target=runtime_owner,
            accepted_fragments=[
                json.dumps(
                    {
                        "runtime_owner": runtime_owner,
                        "orphaned_runtime_ids": list(orphaned_ids),
                        "recovery_required_contract_ids": recovery_required,
                    },
                    sort_keys=True,
                )
            ],
        ),
    )

    # ── trace: runtime_started marker (backward-compatible) ─────────────
    _append(
        store,
        _make_entry(
            store,
            contract_id="runtime_startup",
            event_type=_RUNTIME_STARTED,
            worker_id=worker_id,
            relation_target=runtime_owner,
        ),
    )

    # ── lifecycle: insert / update active row ───────────────────────────
    _upsert_lifecycle(store, runtime_owner, state="active")

    return StartupCheckResult(
        runtime_id=runtime_id,
        runtime_owner=runtime_owner,
        orphaned_runtime_ids=orphaned_ids,
        recovery_required_contract_ids=tuple(recovery_required),
    )


def renew_heartbeat(store, runtime_owner: str) -> None:
    """Renew the heartbeat lease for an active runtime.

    Updates ``heartbeat_at`` in ``runtime_lifecycle`` to the current time,
    keeping the state as ``"active"``.

    Uses ``getattr`` so the call is a no-op on stores that don't support
    runtime lifecycle (e.g. ``MemoryStore``).
    """
    _upsert_lifecycle(store, runtime_owner, state="active")


def end_runtime(store, runtime_owner: str, *, worker_id: str = "runtime") -> None:
    """Append a clean runtime-stop marker and update the lifecycle row.

    The lifecycle state is set to ``'stopped'`` so the lease is no longer
    considered active by future startup checks.
    """
    _append(
        store,
        _make_entry(
            store,
            contract_id="runtime_startup",
            event_type=_RUNTIME_STOPPED,
            worker_id=worker_id,
            relation_target=runtime_owner,
        ),
    )
    _upsert_lifecycle(store, runtime_owner, state="stopped")


# ------------------------------------------------------------------
# internal: lifecycle store helpers
# ------------------------------------------------------------------


def _upsert_lifecycle(store, runtime_owner: str, *, state: str) -> None:
    upsert = getattr(store, "upsert_runtime_lifecycle", None)
    if upsert is None:
        return
    upsert(runtime_owner, now_iso(), state)


# ------------------------------------------------------------------
# internal: trace helpers (unchanged from original pattern)
# ------------------------------------------------------------------


def _all_entries(store) -> list[TraceEntry]:
    get_all = getattr(store, "get_all", None)
    if get_all is None:
        return []
    return list(get_all())


def _make_entry(
    store, contract_id: str, event_type: str, **kwargs: object
) -> TraceEntry:
    return create_entry(
        contract_id=contract_id,
        event_type=event_type,
        sequence=len(_all_entries(store)),
        **kwargs,  # type: ignore[arg-type]
    )


def _append(store, entry: TraceEntry) -> None:
    append = getattr(store, "append", None)
    if append is None:
        return
    append(entry)


# ------------------------------------------------------------------
# internal: stale-contract detection (same logic, different caller)
# ------------------------------------------------------------------


def _has_terminal_trace(contract_id: str, store) -> bool:
    return any(
        entry.event_type in _TERMINAL_EVENTS
        for entry in getattr(store, "get_by_contract", lambda _cid: [])(contract_id)
    )


def _has_expired_claim(contract, store) -> bool:
    """True when a contract has an active claim with an expired lease."""
    if _has_terminal_trace(contract.id, store):
        return False
    get_claim = getattr(store, "get_claim", None)
    if get_claim is None:
        return False
    claim = get_claim(contract.id)
    if claim is None:
        return False
    if claim.get("status") != "active":
        return False
    lease_until = claim.get("lease_until", "")
    if not lease_until:
        return False
    return lease_until < now_iso()
