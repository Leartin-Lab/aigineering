"""Same-machine Worker fleet over independent SQLite connections.

The fleet is an application-layer launcher, not a scheduler. Every slot runs the
normal pull/claim/invoke/submit protocol and derives progress from durable facts.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Condition, Event
import time
import math
from typing import Any
from uuid import uuid4

from aigineering.application import collaboration_service, require_redis
from aigineering.runtime_services import (
    claim_next_application_package,
    order_application_contract_ids,
    sync_runtime,
)
from aigineering.task_views import contract_tree_ids
from aigineering.run_failure_diagnostics import (
    format_worker_failure_detail as _format_worker_failure_detail,
    invocation_failure_detail as _invocation_failure_detail,
)
from aigineering.agent.worker import WorkerHost
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_runtime_publishers
from aigineering.plugins import default_completion_registry
from aigineering.runtime import (
    WorkerInvocationError,
    run_runtime_maintenance_step,
)
from aigineering.task_logs import execute_captured_package
from aigineering.task_views import project_task_status
from aigineering.core.session import runtime_record_contract_references
from collections.abc import Mapping


@dataclass(frozen=True)
class FleetHost:
    """One authenticated WorkerHost and its local execution capacity."""

    host: WorkerHost
    capacity: int = 1
    required_contract_capability: str | None = None

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
    capture_warnings: tuple[str, ...] = ()
    worker_failure_details: tuple[dict[str, Any], ...] = ()
    projection: dict[str, Any] | None = None


@dataclass(frozen=True)
class LocalCycleResult:
    """Outcome of one bounded cycle that admits at most one package globally."""

    contract_id: str | None
    status: str
    completed: bool
    timed_out: bool
    stop_reason: str
    projection: dict[str, Any] | None = None
    cycles: tuple[dict[str, Any], ...] = ()
    submission_status: str | None = None
    worker_errors: tuple[str, ...] = ()
    capture_warnings: tuple[str, ...] = ()
    worker_failure_details: tuple[dict[str, Any], ...] = ()


class _PullPermit:
    def __init__(self, gate: "PullAdmissionGate") -> None:
        self._gate = gate
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._release()


class PullAdmissionGate:
    """Linearize stop against admission of a new claim-pull cycle."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._closed = False
        self._active = 0

    def admit(self) -> _PullPermit | None:
        with self._condition:
            if self._closed:
                return None
            self._active += 1
            return _PullPermit(self)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def wait_drained(self, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._active == 0, timeout)

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

    @property
    def drained(self) -> bool:
        return self.active == 0

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()


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
    require_redis()
    stop = Event()
    errors: Queue[str] = Queue()
    failure_details: Queue[dict[str, Any]] = Queue()
    capture_warnings: list[str] = []
    slot_count = sum(item.capacity for item in hosts)
    deadline = time.monotonic() + timeout
    result_status = "timed_out"
    result_completed = False
    result_timed_out = True
    result_contract_id = target_contract_id

    with ThreadPoolExecutor(max_workers=slot_count) as executor:
        futures = [
            executor.submit(
                _run_slot,
                db_path,
                item.host,
                stop,
                errors,
                poll_interval,
                target_contract_id,
                capture_warnings,
                failure_details,
            )
            for item in hosts
            for _ in range(item.capacity)
        ]
        store = SQLiteStore(db_path)
        try:
            publishers = ensure_local_runtime_publishers(store)
            registry = default_completion_registry()
            while True:
                run_runtime_maintenance_step(
                    store, registry, candidate_publishers=publishers
                )
                sync_runtime(store)
                contract = store.get_contract(target_contract_id)
                if contract is None:
                    raise ValueError(
                        f"fleet target Contract {target_contract_id!r} does not exist"
                    )
                view = RuntimeProjection(store, store).contract_view(contract)
                if view.terminal is not None:
                    stop.set()
                    result_contract_id = contract.id
                    result_status = view.terminal
                    result_completed = view.terminal == "complete"
                    result_timed_out = False
                    break
                if time.monotonic() >= deadline:
                    stop.set()
                    result_contract_id = contract.id
                    result_status = "timed_out"
                    result_completed = False
                    result_timed_out = True
                    break
                if any(future.done() and future.exception() for future in futures):
                    stop.set()
                    result_contract_id = contract.id
                    result_status = "worker_error"
                    result_completed = False
                    result_timed_out = False
                    break
                collaboration_service(store).wait(
                    store.get_runtime_revision(),
                    timeout=min(0.5, max(0.01, poll_interval)),
                )
        finally:
            stop.set()
            store.close()
    # Executor exit joins every slot. Inspect all futures only after that join so
    # terminal/timeout/early-error exits cannot hide a late slot exception.
    for future in futures:
        if future.cancelled():
            continue
        exception = future.exception()
        if exception is not None:
            detail = _slot_failure_detail()
            failure_details.put(detail)
            errors.put(_format_worker_failure_detail(detail))
    store = SQLiteStore(db_path)
    try:
        run_runtime_maintenance_step(
            store,
            default_completion_registry(),
            candidate_publishers=ensure_local_runtime_publishers(store),
        )
        sync_runtime(store)
    finally:
        store.close()
    return FleetRunResult(
        contract_id=result_contract_id,
        status=result_status,
        completed=result_completed,
        timed_out=result_timed_out,
        worker_errors=_drain_errors(errors),
        capture_warnings=tuple(capture_warnings),
        worker_failure_details=_drain_failure_details(failure_details),
        projection=_final_target_projection(db_path, result_contract_id),
    )


def _final_target_projection(db_path: str, contract_id: str) -> dict[str, Any]:
    snapshot = SQLiteStore.snapshot_from_path(db_path)
    try:
        contract = snapshot.get_contract(contract_id)
        if contract is None:
            raise ValueError(f"fleet target Contract {contract_id!r} does not exist")
        return project_task_status(contract, snapshot)
    finally:
        snapshot.close()


def run_local_cycle(
    store,
    hosts: tuple[FleetHost, ...],
    *,
    db_path: str,
    target_contract_id: str | None = None,
    timeout: float | None = None,
) -> LocalCycleResult:
    """Search configured slots until one canonical claim succeeds, then stop.

    A bounded cycle admits and invokes at most one claimed package globally, even
    when several distinct WorkerHosts or capacity slots are supplied. Slots that
    find no eligible package may be followed by later slots. The caller owns the
    selected Store and closes it after this bounded pass. ``timeout`` is a soft
    admission deadline: an admitted synchronous Worker invocation is joined and
    never interrupted.
    """
    if not hosts:
        raise ValueError("local cycle requires at least one WorkerHost")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number")
    deadline = time.monotonic() + float(timeout) if timeout is not None else None
    require_redis()
    publishers = ensure_local_runtime_publishers(store)
    registry = default_completion_registry()
    initial_revision = store.get_runtime_revision()
    maintenance_before = run_runtime_maintenance_step(
        store, registry, candidate_publishers=publishers
    )
    sync_runtime(store)

    target = None
    if target_contract_id is not None:
        target = store.get_contract(target_contract_id)
        if target is None:
            raise ValueError(
                f"cycle target Contract {target_contract_id!r} does not exist"
            )
        target_projection = RuntimeProjection(store, store).contract_view(target)
        if target_projection.terminal is not None:
            projection = project_task_status(target, store)
            sync_runtime(store)
            return LocalCycleResult(
                contract_id=target.id,
                status=projection["status"],
                completed=projection["status"] == "completed",
                timed_out=False,
                stop_reason="terminal",
                projection=projection,
            )

    slots = tuple(host for item in hosts for host in (item,) * item.capacity)
    claims = []
    submissions: list[dict[str, Any]] = []
    errors: list[str] = []
    failure_details: list[dict[str, Any]] = []
    capture_warnings: list[str] = []
    deadline_stopped = False

    for slot in slots:
        if deadline is not None and time.monotonic() >= deadline:
            deadline_stopped = True
            break
        claimed = _claim_cycle_slot(
            store,
            slot.host,
            publishers,
            target_contract_id=target_contract_id,
            required_contract_capability=slot.required_contract_capability,
            request_prefix="local-cycle-claim",
        )
        if claimed is None:
            sync_runtime(store)
            continue
        claims.append(claimed)
        try:
            submissions.append(
                execute_captured_package(
                    claimed,
                    slot.host,
                    store,
                    db_path=db_path,
                    candidate_publishers=publishers,
                    warnings=capture_warnings,
                )
            )
        except WorkerInvocationError:
            detail = _invocation_failure_detail(store, claimed)
            failure_details.append(detail)
            errors.append(_format_worker_failure_detail(detail))
        sync_runtime(store)
        # A bounded cycle admits at most one package. Invocation failure is
        # already an observable outcome and never opens another slot.
        break

    maintenance_after = run_runtime_maintenance_step(
        store, registry, candidate_publishers=publishers
    )
    sync_runtime(store)
    end_revision = store.get_runtime_revision()
    window = store.scan_runtime_records(
        after_revision=initial_revision, through_revision=end_revision
    )
    touched_contracts, unassigned_trace_ids, trace_count = _cycle_trace_scope(window)

    selected_contract = target
    if selected_contract is None and claims:
        selected_contract = store.get_contract(claims[0].contract.id)
    projection = (
        project_task_status(selected_contract, store)
        if selected_contract is not None
        else None
    )
    terminal = bool(projection and projection.get("terminal"))
    completed = bool(projection and projection.get("status") == "completed")
    if failure_details:
        stop_reason = "worker_failure"
    elif terminal:
        stop_reason = "terminal"
    elif deadline_stopped:
        stop_reason = "deadline"
    elif not claims and not any(
        (
            maintenance_before.worker_failures,
            maintenance_before.rejected_submissions,
            maintenance_before.task_completions,
            maintenance_after.worker_failures,
            maintenance_after.rejected_submissions,
            maintenance_after.task_completions,
        )
    ):
        stop_reason = "idle"
    else:
        stop_reason = "cycle_boundary"

    cycle_summaries: list[dict[str, Any]] = []
    if target_contract_id is None:
        if claims:
            cycle_summaries.append(
                {
                    "contracts": [claim.contract.id for claim in claims],
                    "trace_events": sum(
                        len(store.get_by_contract(claim.contract.id))
                        for claim in claims
                    ),
                    "tasks_processed": list(maintenance_before.task_completions)
                    + list(maintenance_after.task_completions),
                    "rejections_recovered": list(
                        maintenance_before.rejected_submissions
                    )
                    + list(maintenance_after.rejected_submissions),
                }
            )
    elif trace_count:
        cycle_summaries.append(
            {
                "contracts": touched_contracts,
                "unassigned_trace_ids": unassigned_trace_ids,
                "trace_events": trace_count,
                "submission_status": submissions[-1].get("status")
                if submissions
                else None,
                "tasks_processed": list(maintenance_before.task_completions)
                + list(maintenance_after.task_completions),
                "rejections_recovered": list(maintenance_before.rejected_submissions)
                + list(maintenance_after.rejected_submissions),
            }
        )

    contract_id = target_contract_id or (claims[0].contract.id if claims else None)
    status = projection["status"] if projection is not None else "idle"
    return LocalCycleResult(
        contract_id=contract_id,
        status=status,
        completed=completed,
        timed_out=deadline_stopped,
        stop_reason=stop_reason,
        projection=projection,
        cycles=tuple(cycle_summaries),
        submission_status=submissions[0].get("status") if submissions else None,
        worker_errors=tuple(errors),
        capture_warnings=tuple(capture_warnings),
        worker_failure_details=tuple(failure_details),
    )


def _run_slot(
    db_path: str,
    host: WorkerHost,
    stop: Event,
    errors: Queue[str],
    poll_interval: float,
    target_contract_id: str,
    capture_warnings: list[str],
    failure_details: Queue[dict[str, Any]],
) -> None:
    store = SQLiteStore(db_path)
    try:
        publishers = ensure_local_runtime_publishers(store)
        while not stop.is_set():
            require_redis()
            claimed = _claim_one(
                store, host, publishers, target_contract_id=target_contract_id
            )
            if claimed is None:
                sync_runtime(store)
                collaboration_service(store).wait(
                    store.get_runtime_revision(),
                    timeout=min(0.5, max(0.01, poll_interval)),
                )
                continue
            try:
                execute_captured_package(
                    claimed,
                    host,
                    store,
                    db_path=db_path,
                    candidate_publishers=publishers,
                    warnings=capture_warnings,
                )
            except WorkerInvocationError:
                # The failure and recovery decision are durable; another task may
                # now be claimable, so the stateless slot continues pulling.
                detail = _invocation_failure_detail(store, claimed)
                failure_details.put(detail)
                errors.put(_format_worker_failure_detail(detail))
            sync_runtime(store)
    finally:
        store.close()


def run_persistent_local_fleet(
    db_path: str,
    hosts: tuple[FleetHost, ...],
    *,
    gate: PullAdmissionGate,
    stop_event: Event,
    stop_requested,
    on_update=None,
    on_tick=None,
    poll_interval: float = 0.1,
) -> tuple[str, ...]:
    """Run domain-wide canonical pull slots until stop or a safe failure."""
    if not hosts:
        raise ValueError("local fleet requires at least one WorkerHost")
    slot_count = sum(item.capacity for item in hosts)
    errors: Queue[str] = Queue()
    capture_warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=slot_count) as executor:
        futures = [
            executor.submit(
                _run_persistent_slot,
                db_path,
                item.host,
                gate,
                stop_event,
                errors,
                poll_interval,
                capture_warnings,
            )
            for item in hosts
            for _ in range(item.capacity)
        ]
        unexpected: BaseException | None = None
        next_tick = time.monotonic()
        try:
            while not all(future.done() for future in futures):
                if stop_requested():
                    gate.close()
                    stop_event.set()
                for future in futures:
                    if future.done() and not future.cancelled():
                        exception = future.exception()
                        if exception is not None and unexpected is None:
                            unexpected = exception
                            gate.close()
                            stop_event.set()
                if on_update is not None:
                    on_update(gate.active)
                now = time.monotonic()
                if on_tick is not None and now >= next_tick:
                    on_tick()
                    next_tick = now + max(0.25, min(1.0, poll_interval * 5))
                time.sleep(0.05)
        finally:
            gate.close()
            stop_event.set()
    # The coordinator loop can be skipped when every future is already done at
    # entry. Inspect the joined futures as well, preserving the first exception
    # observed by the coordinator when it did run.
    if unexpected is None:
        for future in futures:
            if future.cancelled():
                continue
            exception = future.exception()
            if exception is not None:
                unexpected = exception
                break
    if unexpected is not None:
        raise unexpected
    return _drain_errors(errors)


def _run_persistent_slot(
    db_path: str,
    host: WorkerHost,
    gate: PullAdmissionGate,
    stop_event: Event,
    errors: Queue[str],
    poll_interval: float,
    capture_warnings: list[str],
) -> None:
    store = SQLiteStore(db_path)
    try:
        publishers = ensure_local_runtime_publishers(store)
        while not stop_event.is_set():
            permit = gate.admit()
            if permit is None:
                return
            claimed = None
            try:
                require_redis()
                claimed = _claim_one(store, host, publishers, target_contract_id=None)
                if claimed is not None:
                    try:
                        execute_captured_package(
                            claimed,
                            host,
                            store,
                            db_path=db_path,
                            candidate_publishers=publishers,
                            warnings=capture_warnings,
                        )
                    except WorkerInvocationError:
                        # The runtime has durably recorded failure/recovery; this
                        # is an invocation outcome, not a service process error.
                        detail = _invocation_failure_detail(store, claimed)
                        errors.put(_format_worker_failure_detail(detail))
                    sync_runtime(store)
            finally:
                permit.release()
            if claimed is None:
                # Bound the wait so a local stop request closes admission even
                # if a wake notification is missed.
                collaboration_service(store).wait(
                    store.get_runtime_revision(),
                    timeout=min(0.5, max(0.01, poll_interval)),
                )
    finally:
        store.close()


def _claim_one(store, host, publishers, *, target_contract_id: str | None):
    return _claim_cycle_slot(
        store,
        host,
        publishers,
        target_contract_id=target_contract_id,
        required_contract_capability=None,
        request_prefix="fleet-claim",
    )


def _claim_cycle_slot(
    store,
    host,
    publishers,
    *,
    target_contract_id: str | None,
    required_contract_capability: str | None,
    request_prefix: str,
):
    if target_contract_id is None and required_contract_capability is None:
        return claim_next_application_package(
            store,
            worker_id=host.worker_id,
            candidate_publishers=publishers,
            claim_candidate=host.claim_candidate(
                request_id=f"{request_prefix}:{uuid4().hex}"
            ),
            expected_registration_version=host.registration_version,
        )
    contracts = store.get_all_contracts()
    allowed = (
        contract_tree_ids(contracts, target_contract_id)
        if target_contract_id is not None
        else {item.id for item in contracts}
    )
    if required_contract_capability is not None:
        allowed = {
            item.id
            for item in contracts
            if item.id in allowed
            and required_contract_capability in item.worker_capabilities
        }
    for contract_id in order_application_contract_ids(store, tuple(sorted(allowed))):
        claimed = claim_next_application_package(
            store,
            worker_id=host.worker_id,
            contract_id=contract_id,
            candidate_publishers=publishers,
            claim_candidate=host.claim_candidate(
                request_id=f"{request_prefix}:{uuid4().hex}",
                contract_id=contract_id,
            ),
            expected_registration_version=host.registration_version,
        )
        if claimed is not None:
            return claimed
    return None


def _cycle_trace_scope(window):
    records = [record for _revision, record in window]
    records_by_id = {record.id: record for record in records}
    traces = [record for record in records if record.record_type == "trace.recorded"]
    contracts: set[str] = set()
    unassigned: list[str] = []
    for record in traces:
        refs = runtime_record_contract_references(record, records_by_id)
        contracts.update(refs)
        if not refs:
            trace = record.payload.get("trace")
            if isinstance(trace, Mapping) and isinstance(trace.get("id"), str):
                unassigned.append(trace["id"])
    return sorted(contracts), sorted(unassigned), len(traces)


def _drain_errors(errors: Queue[str]) -> tuple[str, ...]:
    values: list[str] = []
    while True:
        try:
            values.append(errors.get_nowait())
        except Empty:
            return tuple(values)


def _drain_failure_details(
    details: Queue[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    while True:
        try:
            values.append(details.get_nowait())
        except Empty:
            return tuple(values)


def _slot_failure_detail() -> dict[str, Any]:
    """Describe an unbound slot exception without exposing its exception text."""
    return {
        "kind": "slot_error",
        "status": "unexpected_exception",
        "category": "unexpected_exception",
    }
