"""Capability-based selection of stateless execution workers.

Routing is control-plane policy, deliberately separate from ``behavior:*``
prompt labels.  The functions here are pure so eligibility can be tested and
audited without an Engine or provider dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aigineering.protocol.types import Contract


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

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        if self.capacity < 1:
            raise ValueError("capacity must be at least 1")
        if self.active_claims < 0:
            raise ValueError("active_claims must not be negative")
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        object.__setattr__(self, "pools", tuple(sorted(set(self.pools))))


def is_eligible(contract: Contract, worker: WorkerRegistration) -> bool:
    """Return whether *worker* satisfies all hard routing constraints."""
    if not worker.enabled or worker.active_claims >= worker.capacity:
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
    """Select one eligible worker without embedding provider policy in Engine."""
    eligible = eligible_workers(contract, workers)
    return eligible[0] if eligible else None
