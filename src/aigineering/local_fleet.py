"""Same-machine Worker fleet over independent SQLite connections.

The fleet is an application-layer launcher, not a scheduler. Every slot runs the
normal pull/claim/invoke/submit protocol and derives progress from durable facts.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event
import time

from aigineering.agent.worker import WorkerHost
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_runtime_publishers
from aigineering.plugins import default_completion_registry
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_rejected_submissions,
    process_task_completions,
    process_worker_failures,
)


@dataclass(frozen=True)
class FleetHost:
    """One authenticated WorkerHost and its local execution capacity."""

    host: WorkerHost
    capacity: int = 1

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("FleetHost capacity must be at least 1")


@dataclass(frozen=True)
class FleetRunResult:
    """Bounded fleet outcome projected from the authoritative Store."""

    contract_id: str
    status: str
    completed: bool
    timed_out: bool
    worker_errors: tuple[str, ...] = ()


def run_local_fleet(
    db_path: str,
    hosts: tuple[FleetHost, ...],
    *,
    target_contract_id: str,
    timeout: float = 300.0,
    poll_interval: float = 0.1,
) -> FleetRunResult:
    """Run independent local Worker slots until one target becomes terminal."""
    if not hosts:
        raise ValueError("local fleet requires at least one WorkerHost")
    stop = Event()
    errors: Queue[str] = Queue()
    slot_count = sum(item.capacity for item in hosts)
    deadline = time.monotonic() + timeout

    with ThreadPoolExecutor(max_workers=slot_count) as executor:
        futures = [
            executor.submit(
                _run_slot,
                db_path,
                item.host,
                stop,
                errors,
                poll_interval,
            )
            for item in hosts
            for _ in range(item.capacity)
        ]
        store = SQLiteStore(db_path)
        try:
            publishers = ensure_local_runtime_publishers(store)
            registry = default_completion_registry()
            while True:
                process_worker_failures(store, candidate_publishers=publishers)
                process_rejected_submissions(store, candidate_publishers=publishers)
                process_task_completions(
                    store, registry, candidate_publishers=publishers
                )
                contract = store.get_contract(target_contract_id)
                if contract is None:
                    raise ValueError(
                        f"fleet target Contract {target_contract_id!r} does not exist"
                    )
                view = RuntimeProjection(store, store).contract_view(contract)
                if view.terminal is not None:
                    stop.set()
                    return FleetRunResult(
                        contract_id=contract.id,
                        status=view.terminal,
                        completed=view.terminal == "complete",
                        timed_out=False,
                        worker_errors=_drain_errors(errors),
                    )
                if time.monotonic() >= deadline:
                    stop.set()
                    return FleetRunResult(
                        contract_id=contract.id,
                        status="timed_out",
                        completed=False,
                        timed_out=True,
                        worker_errors=_drain_errors(errors),
                    )
                if any(future.done() and future.exception() for future in futures):
                    stop.set()
                    for future in futures:
                        if future.done() and future.exception() is not None:
                            errors.put(str(future.exception()))
                    return FleetRunResult(
                        contract_id=contract.id,
                        status="worker_error",
                        completed=False,
                        timed_out=False,
                        worker_errors=_drain_errors(errors),
                    )
                stop.wait(max(0.01, poll_interval))
        finally:
            stop.set()
            store.close()


def _run_slot(
    db_path: str,
    host: WorkerHost,
    stop: Event,
    errors: Queue[str],
    poll_interval: float,
) -> None:
    store = SQLiteStore(db_path)
    try:
        publishers = ensure_local_runtime_publishers(store)
        while not stop.is_set():
            claimed = claim_next_package(
                store,
                worker_id=host.worker_id,
                candidate_publishers=publishers,
            )
            if claimed is None:
                stop.wait(max(0.01, poll_interval))
                continue
            try:
                execute_claimed_package(
                    claimed,
                    host,
                    store,
                    candidate_publishers=publishers,
                )
            except WorkerInvocationError as exc:
                # The failure and recovery decision are durable; another task may
                # now be claimable, so the stateless slot continues pulling.
                errors.put(str(exc))
    finally:
        store.close()


def _drain_errors(errors: Queue[str]) -> tuple[str, ...]:
    values: list[str] = []
    while True:
        try:
            values.append(errors.get_nowait())
        except Empty:
            return tuple(values)
