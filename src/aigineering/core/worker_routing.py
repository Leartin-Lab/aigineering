"""Capability-based selection of stateless execution workers.

Routing is control-plane policy, deliberately separate from ``behavior:*``
prompt labels.  The functions here are pure so eligibility can be tested and
audited without an Engine or provider dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aigineering.protocol.types import Contract
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.core.record_conflict import ImmutableRecordConflict


EXCLUSIVE_EXECUTION_CAPABILITIES = frozenset({"tool-execution", "mcp-execution"})


@dataclass(frozen=True)
class WorkerRegistration:
    """Trusted description of one execution worker.

    ``capabilities`` and ``pools`` describe scheduling eligibility only. They
    are never prompt assets and grant no additional minting authority.
    """

    worker_id: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    pools: tuple[str, ...] = field(default_factory=tuple)
    profile_id: str = ""
    capacity: int = 1
    active_claims: int = 0
    enabled: bool = True
    version: str = "1"
    actor_id: str = ""
    key_id: str = ""

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        if self.capacity < 1:
            raise ValueError("capacity must be at least 1")
        if self.active_claims < 0:
            raise ValueError("active_claims must not be negative")
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        object.__setattr__(self, "pools", tuple(sorted(set(self.pools))))


def worker_registration_payload(registration: WorkerRegistration) -> dict:
    return {
        "capabilities": list(registration.capabilities),
        "capacity": registration.capacity,
        "enabled": registration.enabled,
        "pools": list(registration.pools),
        "profile_id": registration.profile_id,
        "version": registration.version,
        "worker_id": registration.worker_id,
        "actor_id": registration.actor_id,
        "key_id": registration.key_id,
    }


def worker_registration_record(registration: WorkerRegistration) -> RuntimeRecord:
    """Return the immutable control-plane fact for a registration version."""
    return create_runtime_record(
        "worker.registered", worker_registration_payload(registration)
    )


def registration_from_record(
    record: RuntimeRecord, *, active_claims: int = 0
) -> WorkerRegistration:
    """Materialize a routing view from a ``worker.registered`` fact."""
    if record.record_type != "worker.registered":
        raise ValueError(f"expected worker.registered, got {record.record_type!r}")
    payload = record.payload
    return WorkerRegistration(
        worker_id=str(payload["worker_id"]),
        capabilities=tuple(payload["capabilities"]),
        pools=tuple(payload["pools"]),
        profile_id=str(payload["profile_id"]),
        capacity=int(payload["capacity"]),
        active_claims=active_claims,
        enabled=bool(payload["enabled"]),
        version=str(payload["version"]),
        actor_id=str(payload.get("actor_id", "")),
        key_id=str(payload.get("key_id", "")),
    )


def registration_is_replay(
    records: list[tuple[int, RuntimeRecord]], registration: WorkerRegistration
) -> bool:
    """Validate immutable worker/version identity and report exact replay."""
    for _, record in records:
        existing = registration_from_record(record)
        if (
            existing.worker_id == registration.worker_id
            and existing.version == registration.version
        ):
            if existing == registration:
                return True
            raise ImmutableRecordConflict(
                "worker registration version",
                f"{registration.worker_id}:{registration.version}",
            )
    return False


def is_eligible(contract: Contract, worker: WorkerRegistration) -> bool:
    """Return whether *worker* satisfies all hard routing constraints."""
    if not worker.enabled or worker.active_claims >= worker.capacity:
        return False
    exclusive = set(worker.capabilities) & EXCLUSIVE_EXECUTION_CAPABILITIES
    if exclusive and not exclusive.intersection(contract.worker_capabilities):
        return False
    if not set(contract.worker_capabilities).issubset(worker.capabilities):
        return False
    return not contract.worker_pools or bool(
        set(contract.worker_pools) & set(worker.pools)
    )


def eligible_workers(
    contract: Contract,
    workers: list[WorkerRegistration] | tuple[WorkerRegistration, ...],
) -> tuple[WorkerRegistration, ...]:
    """Return eligible workers in deterministic least-loaded/id order."""
    return tuple(
        sorted(
            (worker for worker in workers if is_eligible(contract, worker)),
            key=lambda worker: (worker.active_claims, worker.worker_id),
        )
    )


def select_worker(
    contract: Contract,
    workers: list[WorkerRegistration] | tuple[WorkerRegistration, ...],
) -> WorkerRegistration | None:
    """Select one eligible worker without embedding provider policy in core."""
    eligible = eligible_workers(contract, workers)
    return eligible[0] if eligible else None
