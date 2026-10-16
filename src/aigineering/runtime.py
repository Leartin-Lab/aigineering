"""Application-neutral worker protocol execution and recovery services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
import sqlite3
import time
from threading import Event, Thread
from uuid import uuid4

from aigineering.agent.llm import LLMWorker, ProviderError
from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerExecutionError, WorkerHost
from aigineering.core.activation import check_activation
from aigineering.core.disclosure import (
    DisclosurePolicyError,
    compute_disclosure,
)
from aigineering.core.store import require_operational_store
from aigineering.plugins.completion_projection import (
    TaskCompletionContext,
    TaskCompletionProjector,
)
from aigineering.plugins.recovery import schedule_projection_recovery
from aigineering.plugins.task_semantics import method_payload
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.commitment import (
    CandidateCommitRejected,
    CandidateCommitter,
    record_candidate_rejection,
)
from aigineering.core.candidate_decision import trace_record
from aigineering.core.worker_routing import is_eligible
from aigineering.core.worker_coordination import (
    authenticate_worker_command,
    same_worker_command_records,
)
from aigineering.core.trace_manager import TraceManager
from aigineering.core.trace import create_entry
from aigineering.protocol.candidate import CandidateClaimBinding
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.usage import normalize_usage_metadata
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_to_dict,
)
from aigineering.task_context import method_context_assets_for


def _capture_emit(observer, event: str, **fields) -> None:
    """Keep local diagnostic capture outside normal Worker semantics."""
    if observer is None:
        return
    try:
        observer.emit(event, **fields)
    except Exception:
        # A capture callback is strictly best-effort and cannot affect execution.
        return


@dataclass(frozen=True)
class ClaimedPackage:
    contract: Contract
    disclosed_assets: tuple[Asset, ...]
    method_context_assets: tuple[Asset, ...]
    package: WorkerPackage
    worker_id: str


@dataclass(frozen=True)
class RuntimeMaintenanceResult:
    """Durable work observed by one stateless runtime maintenance pass."""

    worker_failures: tuple[str, ...]
    rejected_submissions: tuple[str, ...]
    task_completions: tuple[str, ...]


def run_runtime_maintenance_step(
    store, completion_registry, *, candidate_publishers=None
) -> RuntimeMaintenanceResult:
    """Run the canonical, stateless maintenance sequence once."""
    worker_failures = process_worker_failures(
        store, candidate_publishers=candidate_publishers
    )
    rejected_submissions = process_rejected_submissions(
        store, candidate_publishers=candidate_publishers
    )
    task_completions = process_task_completions(
        store,
        completion_registry,
        candidate_publishers=candidate_publishers,
    )
    return RuntimeMaintenanceResult(
        worker_failures=tuple(worker_failures),
        rejected_submissions=tuple(rejected_submissions),
        task_completions=tuple(task_completions),
    )


class WorkerInvocationError(RuntimeError):
    """A claimed provider call failed and was durably closed."""


class WorkerSubmissionCommitError(RuntimeError):
    """A valid Candidate could not be transactionally committed."""


class _ClaimLeaseKeeper:
    """Renew one claim while its synchronous provider invocation is running."""

    def __init__(self, claimed: ClaimedPackage, store, worker_host=None) -> None:
        self._claimed = claimed
        self._store = store
        self._worker_host = worker_host
        self._stop = Event()
        self._failed = Event()
        self._thread: Thread | None = None
        try:
            deadline = datetime.fromisoformat(claimed.package.lease_until)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            lease_seconds = max(
                1, int((deadline - datetime.now(timezone.utc)).total_seconds())
            )
        except (TypeError, ValueError):
            lease_seconds = 60
        self._lease_seconds = lease_seconds
        self._interval = max(0.1, min(10.0, lease_seconds / 3))

    def start(self) -> None:
        if not self._claimed.package.claim_id or self._worker_host is None:
            return
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._interval * 2))
            return not self._thread.is_alive()
        return True

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if self._worker_host is None:
                    self._failed.set()
                    return
                renewal = self._worker_host.renewal_candidate(
                    self._claimed.package,
                    request_id=f"renew:{self._claimed.package.claim_id}:{uuid4().hex}",
                    lease_seconds=self._lease_seconds,
                )
                renewed = self._store.renew_claim(
                    self._claimed.package.claim_id,
                    self._claimed.package.claim_epoch,
                    self._claimed.worker_id,
                    lease_seconds=self._lease_seconds,
                    claim_candidate=renewal,
                )
            except (RuntimeError, sqlite3.Error, TypeError, ValueError, LookupError):
                self._failed.set()
                return
            if renewed is None:
                self._failed.set()
                return


def claim_next_package(
    store,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    contract_id: str | None = None,
    expected_registration_version: str | None = None,
    candidate_publishers=None,
    claim_runtime_records=(),
    claim_candidate=None,
    order_contract_ids: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None,
) -> ClaimedPackage | None:
    """Claim the next ready contract and return its worker package."""
    store = require_operational_store(store)
    if claim_candidate is not None:
        command = authenticate_worker_command(claim_candidate, "worker.claim", store)
        payload = command.payload
        if (
            payload["worker_id"] != worker_id
            or payload["lease_seconds"] != lease_seconds
            or (
                payload["contract_id"] is not None
                and payload["contract_id"] != contract_id
            )
        ):
            raise ValueError("signed worker.claim does not match the package scan")
        if claim_runtime_records and not same_worker_command_records(
            tuple(claim_runtime_records), command.runtime_records
        ):
            raise ValueError("claim runtime records do not match the signed Candidate")
        claim_runtime_records = command.runtime_records
    registered_worker = store.get_worker_registration(worker_id)
    selected_registration_version = (
        registered_worker.version if registered_worker is not None else ""
    )
    if (
        expected_registration_version not in (None, "")
        and expected_registration_version != selected_registration_version
    ):
        raise ValueError(
            f"WorkerHost registration version {expected_registration_version!r} is "
            f"stale for {worker_id!r}; current version is "
            f"{selected_registration_version or 'unregistered'}"
        )
    registration_version = (
        selected_registration_version
        if expected_registration_version in (None, "")
        else expected_registration_version
    )
    process_expired_claims(store, candidate_publishers=candidate_publishers)
    # Maintenance can expire claims and therefore change the capacity snapshot.
    registered_worker = store.get_worker_registration(worker_id)
    if contract_id is not None:
        selected = store.get_contract(contract_id)
        if selected is None or not _worker_can_claim(selected, registered_worker):
            return None
        contracts = [selected]
    else:
        contracts = store.get_all_contracts()
    routed_contracts = [
        contract
        for contract in contracts
        if _worker_can_claim(contract, registered_worker)
    ]
    if not routed_contracts:
        return None
    from aigineering.core.claim_assignments import (
        assignment_context_from_projection,
        effective_assignment,
    )

    assignment_context = assignment_context_from_projection(
        EffectProjectionContext(
            runtime_records=tuple(record for _, record in store.scan_runtime_records())
        )
    )
    policy_blockers: list[DisclosurePolicyError] = []
    routed_contracts = [
        contract
        for contract in routed_contracts
        if (
            (
                assigned := effective_assignment(
                    assignment_context, contract_id=contract.id
                )
            )
            is None
            or assigned.payload.get("worker_id") == worker_id
        )
    ]
    if not routed_contracts:
        return None
    if contract_id is None and order_contract_ids is not None:
        if not callable(order_contract_ids):
            raise ValueError("claim order hook must be callable")
        routed_contracts = _reorder_routed_contracts(
            routed_contracts, order_contract_ids
        )
    available_names = {a.name for a in store.get_all_assets()}
    projection = RuntimeProjection(
        store,
        store,
        runtime_records=tuple(store.scan_runtime_records()),
        available_names=available_names,
    )
    for contract in routed_contracts:
        if contract.activation and not check_activation(
            contract.activation, available_names
        ):
            continue
        view = projection.contract_view(contract)
        if not view.enabled:
            continue

        remaining_budget = view.budget_remaining

        try:
            disclosed = tuple(compute_disclosure(contract, store))
        except DisclosurePolicyError as exc:
            policy_blockers.append(exc)
            store.new_entry(
                contract.id,
                "disclosure_policy_rejected",
                rejected_fragments=list(exc.reasons),
                authority_result="rejected",
            )
            continue
        method_context_assets = _method_context_assets_for(contract, store)
        package = _build_worker_package(
            contract,
            disclosed,
            method_context_assets,
            registered_worker,
            remaining_budget,
        )
        claim = store.claim_contract(
            contract.id,
            worker_id,
            lease_seconds=lease_seconds,
            package_id=package.package_id,
            expected_registration_version=(registration_version),
            runtime_records=tuple(claim_runtime_records),
            claim_candidate=claim_candidate,
        )
        if claim is None:
            current_registration = store.get_worker_registration(worker_id)
            current_version = (
                current_registration.version if current_registration is not None else ""
            )
            if current_version != registration_version:
                raise ValueError(
                    f"WorkerHost registration changed during claim preparation for "
                    f"{worker_id!r}; expected {registration_version or 'unregistered'}, "
                    f"current is {current_version or 'unregistered'}"
                )
            continue
        package = _record_worker_route(
            store,
            contract,
            package,
            claim,
            worker_id,
            registered_worker,
            remaining_budget,
        )
        return ClaimedPackage(
            contract, disclosed, method_context_assets, package, worker_id
        )
    if policy_blockers:
        reasons = [reason for exc in policy_blockers for reason in exc.reasons]
        raise DisclosurePolicyError(policy_blockers[0].contract_id, reasons)
    return None


def _reorder_routed_contracts(
    contracts: list[Contract],
    order_contract_ids: Callable[[tuple[str, ...]], tuple[str, ...]],
) -> list[Contract]:
    """Apply a complete ordering to already-routed contracts only."""
    contract_ids = tuple(contract.id for contract in contracts)
    if any(
        not isinstance(contract_id, str) or not contract_id
        for contract_id in contract_ids
    ):
        raise ValueError("claim scan contains an invalid Contract ID")
    if len(set(contract_ids)) != len(contract_ids):
        raise ValueError("claim scan contains duplicate Contract IDs")
    ordered_ids = order_contract_ids(contract_ids)
    if not isinstance(ordered_ids, tuple) or any(
        not isinstance(contract_id, str) or not contract_id
        for contract_id in ordered_ids
    ):
        raise ValueError("claim order hook must return a tuple of Contract IDs")
    if (
        len(ordered_ids) != len(contract_ids)
        or len(set(ordered_ids)) != len(ordered_ids)
        or set(ordered_ids) != set(contract_ids)
    ):
        raise ValueError("claim order hook must return a complete Contract permutation")
    by_id = {contract.id: contract for contract in contracts}
    return [by_id[item] for item in ordered_ids]


def _worker_can_claim(contract: Contract, registration) -> bool:
    """Apply capability routing without turning labels into authority."""
    if contract.worker_capabilities or contract.worker_pools:
        return registration is not None and is_eligible(contract, registration)
    return registration is None or is_eligible(contract, registration)


def _build_worker_package(
    contract: Contract,
    disclosed: tuple[Asset, ...],
    method_context_assets: tuple[Asset, ...],
    registration,
    remaining_budget: int,
) -> WorkerPackage:
    return WorkerPackage(
        contract_id=contract.id,
        contract=contract_to_dict(contract),
        disclosed_assets=tuple(asset_to_dict(asset) for asset in disclosed),
        method_context_assets=tuple(
            asset_to_dict(asset) for asset in method_context_assets
        ),
        tool_scope=contract.tool_scope,
        budget_remaining=remaining_budget,
        capability_requirements=contract.worker_capabilities,
        worker_profile_id=registration.profile_id if registration else "",
        worker_registration_version=registration.version if registration else "",
    )


def _record_worker_route(
    store,
    contract: Contract,
    package: WorkerPackage,
    claim: dict,
    worker_id: str,
    registration,
    remaining_budget: int,
) -> WorkerPackage:
    routed = replace(
        package,
        claim_id=claim["claim_id"],
        claim_epoch=claim["epoch"],
        lease_until=claim["lease_until"],
    )
    store.new_entry(
        contract.id,
        "worker_routed",
        worker_id=worker_id,
        relation_type="worker_profile",
        relation_target=(
            f"{registration.profile_id}@{registration.version}"
            if registration
            else "legacy"
        ),
        budget_remaining=remaining_budget,
    )
    return routed


def execute_claimed_package(
    claimed: ClaimedPackage,
    worker: MockWorker | LLMWorker | WorkerHost,
    store,
    trace_store=None,
    candidate_publishers=None,
    event_observer=None,
) -> dict:
    """Invoke an authenticated WorkerHost and submit its ordinary effects."""
    trace = trace_store if trace_store is not None else store
    keeper = _ClaimLeaseKeeper(
        claimed,
        store,
        worker if isinstance(worker, WorkerHost) else None,
    )
    keeper.start()
    candidate = None
    try:
        phase = "invocation"
        try:
            invocation_binding = CandidateClaimBinding(
                contract_id=claimed.contract.id,
                claim_id=claimed.package.claim_id,
                claim_epoch=claimed.package.claim_epoch,
                package_id=claimed.package.package_id,
            )
            disclosed = list(claimed.disclosed_assets + claimed.method_context_assets)
            from aigineering.task_logs import observer_scope

            invocation_started = time.monotonic()
            _capture_emit(event_observer, "invocation.started")
            try:
                with observer_scope(event_observer):
                    candidate = (
                        worker.invoke(
                            claimed.contract,
                            disclosed,
                            claim_binding=invocation_binding,
                        )
                        if isinstance(worker, WorkerHost)
                        else worker.invoke(claimed.contract, disclosed)
                    )
                _capture_emit(event_observer, "candidate.returned")
            except (ProviderError, WorkerExecutionError) as exc:
                _capture_emit(
                    event_observer,
                    "invocation.failed",
                    category=(
                        "provider_error"
                        if isinstance(exc, ProviderError)
                        else "worker_execution_error"
                    ),
                    status_code=(
                        exc.status_code if isinstance(exc, ProviderError) else 0
                    ),
                    retryable=(
                        exc.is_retryable if isinstance(exc, ProviderError) else False
                    ),
                )
                raise
            except Exception:
                _capture_emit(
                    event_observer,
                    "invocation.failed",
                    category="other",
                    status_code=0,
                    retryable=False,
                )
                raise
            finally:
                _capture_emit(
                    event_observer,
                    "invocation.finished",
                    outcome="returned" if candidate is not None else "failed",
                    duration_ms=max(
                        0, int((time.monotonic() - invocation_started) * 1000)
                    ),
                )
            phase = "envelope"
            envelope = CandidateEnvelope(
                contract_id=claimed.contract.id,
                worker_id=claimed.worker_id,
                raw_output=candidate.raw_output,
                parsed_action=(
                    dict(candidate.parsed_action)
                    if candidate.parsed_action is not None
                    else None
                ),
                package_id=claimed.package.package_id,
                claim_id=claimed.package.claim_id,
                claim_epoch=claimed.package.claim_epoch,
                idempotency_key=f"run-{claimed.package.package_id}",
                usage_metadata=normalize_usage_metadata(candidate.metadata),
            )
            phase = "candidate_encoding"
            proposal = (
                worker.sign_envelope(
                    envelope,
                    contract=claimed.contract,
                    disclosed_assets=claimed.disclosed_assets
                    + claimed.method_context_assets,
                    allowance=claimed.package.budget_remaining,
                )
                if isinstance(worker, WorkerHost)
                else None
            )
        except (ProviderError, WorkerExecutionError) as exc:
            if keeper.stop() is False:
                raise WorkerSubmissionCommitError(
                    "claim renewal thread did not stop; refusing concurrent failure write"
                ) from exc
            status_code = exc.status_code if isinstance(exc, ProviderError) else 0
            retryable = exc.is_retryable if isinstance(exc, ProviderError) else False
            category = (
                "provider_error"
                if isinstance(exc, ProviderError)
                else f"worker_error:{exc.code}"
            )
            _record_worker_invocation_failure(
                claimed,
                store,
                status_code=status_code,
                retryable=retryable,
                category=category,
                raw_output=(
                    candidate.raw_output
                    if isinstance(exc, WorkerExecutionError) and candidate is not None
                    else None
                ),
                usage_metadata=(
                    exc.usage_metadata
                    if isinstance(exc, WorkerExecutionError)
                    and exc.usage_metadata is not None
                    else candidate.metadata
                    if candidate is not None
                    else None
                ),
                diagnostic=(
                    str(exc) if isinstance(exc, WorkerExecutionError) else None
                ),
            )
            if candidate_publishers is not None:
                process_worker_failures(
                    store, candidate_publishers=candidate_publishers
                )
            raise WorkerInvocationError(
                f"worker invocation failed with status {status_code}; "
                "claim was released and recovery was evaluated"
            ) from None
        except (RuntimeError, TypeError, ValueError) as exc:
            if keeper.stop() is False:
                raise WorkerSubmissionCommitError(
                    "claim renewal thread did not stop; refusing concurrent failure write"
                ) from exc
            candidate_processing_failure = (
                phase in {"envelope", "candidate_encoding"} and candidate is not None
            )
            _record_worker_invocation_failure(
                claimed,
                store,
                status_code=0,
                retryable=False,
                category=f"worker_error:{phase}_failure",
                raw_output=(
                    candidate.raw_output if candidate_processing_failure else None
                ),
                usage_metadata=(
                    candidate.metadata if candidate_processing_failure else None
                ),
                diagnostic=(str(exc) if candidate_processing_failure else None),
            )
            if candidate_publishers is not None:
                process_worker_failures(
                    store, candidate_publishers=candidate_publishers
                )
            raise WorkerInvocationError(
                f"worker {phase} failed; claim was released and recovery was evaluated"
            ) from None
    finally:
        keeper_stopped = keeper.stop()
    if keeper_stopped is False:
        raise WorkerSubmissionCommitError(
            "claim renewal thread did not stop; refusing to reuse its Store connection"
        )
    if keeper.failed:
        try:
            _record_worker_invocation_failure(
                claimed,
                store,
                status_code=0,
                retryable=True,
                category="worker_error:claim_renewal_failed",
                usage_metadata=candidate.metadata if candidate is not None else None,
            )
        except sqlite3.Error as exc:
            raise WorkerSubmissionCommitError(
                "claim lease renewal failed and its terminal fact could not be "
                f"committed: {exc}"
            ) from exc
        if candidate_publishers is not None:
            process_worker_failures(store, candidate_publishers=candidate_publishers)
        raise WorkerInvocationError(
            f"claim lease renewal failed for {claimed.package.claim_id!r}; "
            "worker result was discarded and the claim was durably closed"
        )
    if proposal is None:
        _record_worker_invocation_failure(
            claimed,
            store,
            status_code=0,
            retryable=False,
            category="worker_error:unsigned_adapter",
            usage_metadata=candidate.metadata if candidate is not None else None,
        )
        if candidate_publishers is not None:
            process_worker_failures(store, candidate_publishers=candidate_publishers)
        raise WorkerInvocationError(
            "claimed execution requires an authenticated WorkerHost; "
            "claim was released and recovery was evaluated"
        )
    return submit_worker_proposal(
        proposal,
        store,
        trace_store=trace,
        candidate_publishers=candidate_publishers,
    )


def submit_worker_proposal(
    proposal,
    store,
    *,
    trace_store=None,
    candidate_publishers=None,
) -> dict:
    """Submit one claim-bound ordinary-effect Candidate."""
    trace = trace_store if trace_store is not None else store
    if proposal.claim_binding is None:
        raise ValueError("worker Candidate requires an explicit claim binding")
    duplicate = any(
        record.payload.get("candidate_id") == proposal.id
        for _, record in store.scan_runtime_records(record_type="candidate.committed")
    )
    try:
        decision = CandidateCommitter(store, trace).commit(proposal)
    except CandidateCommitRejected:
        raise
    except sqlite3.IntegrityError as exc:
        record_candidate_rejection(proposal, str(exc), store, trace)
        raise ValueError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        record_candidate_rejection(proposal, str(exc), store, trace)
        raise
    except sqlite3.Error as exc:
        raise WorkerSubmissionCommitError(
            f"Candidate commitment infrastructure failed: {exc}"
        ) from exc
    if not decision.accepted and candidate_publishers is not None:
        process_rejected_submissions(store, candidate_publishers=candidate_publishers)
    terminal = any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == proposal.claim_binding.contract_id
        and record.payload.get("terminal") == "complete"
        for record in decision.runtime_records
    )
    if decision.contracts:
        first = decision.contracts[0]
        declared_method = str(method_payload(first).get("method", ""))
        return {
            "contract_id": proposal.claim_binding.contract_id,
            "status": "task_delegated" if decision.accepted else "rejected",
            "method": declared_method or "task_expansion",
            "child_contract_id": first.id if len(decision.contracts) == 1 else None,
            "child_contract_ids": [item.id for item in decision.contracts],
            "complete": False,
            "duplicate": duplicate,
        }
    rejection_reasons = [
        str(record.payload.get("reason", "candidate rejected"))
        for record in decision.runtime_records
        if record.record_type
        in {"candidate.rejected", "candidate.authentication_rejected"}
    ]
    return {
        "contract_id": proposal.claim_binding.contract_id,
        "status": "accepted" if decision.accepted else "rejected",
        "accepted": [asset.name for asset in decision.assets],
        "rejected": rejection_reasons,
        "complete": terminal,
        "duplicate": duplicate,
    }


def _schedule_rejected_recovery(
    contract: Contract,
    candidate_raw: str,
    rejections: list[dict],
    store,
    trace,
    *,
    candidate_publishers=None,
    source_binding=None,
) -> Contract | None:
    if candidate_publishers is None:
        raise RuntimeError(
            "recovery replay requires an authenticated recovery Candidate publisher"
        )
    trace_manager = TraceManager(trace)
    runtime = TaskCompletionContext(store, trace_manager, candidate_publishers)
    ancestor_source_bindings = None
    recovery_snapshot = None
    if source_binding is not None:
        from aigineering.task_diagnostics import TaskDiagnosticError

        snapshot = runtime.capture_recovery_history()
        recovery_snapshot = snapshot
        if snapshot is None or snapshot.query_context is None:
            raise TaskDiagnosticError("recovery_source_snapshot_unavailable")
        from aigineering.task_diagnostics import (
            task_recovery_ancestor_source_bindings,
            task_recovery_source_binding,
        )

        query = snapshot.query_context
        source_record_id = source_binding.get("source_record_id")
        claim_id = source_binding.get("claim_id")
        source_kind = source_binding.get("kind")
        if not all(
            isinstance(value, str) and value
            for value in (source_record_id, claim_id, source_kind)
        ):
            from aigineering.task_diagnostics import TaskDiagnosticError

            raise TaskDiagnosticError("recovery_source_integrity_error")
        proven_binding = task_recovery_source_binding(
            store,
            contract.id,
            claim_id,
            source_record_id,
            source_kind,
            query_context=query,
        )
        for key in (
            "kind",
            "contract_id",
            "claim_id",
            "grant_record_id",
            "failure_record_id",
            "release_record_id",
            "source_record_id",
            "witness_record_ids",
        ):
            if source_binding.get(key) != proven_binding.get(key):
                from aigineering.task_diagnostics import TaskDiagnosticError

                raise TaskDiagnosticError("recovery_source_snapshot_mismatch")
        source_binding = proven_binding
        try:
            ancestor_source_bindings = task_recovery_ancestor_source_bindings(
                store, contract.id, query_context=query
            )
        except TaskDiagnosticError:
            # The current failure is authenticated, so malformed prior history
            # is an unsupported method disposition; it cannot authorize work.
            ancestor_source_bindings = {}
    recovery = schedule_projection_recovery(
        runtime,
        failed_contract=contract,
        candidate_raw=candidate_raw,
        rejections=rejections,
        source_binding=source_binding,
        ancestor_source_bindings=ancestor_source_bindings,
        recovery_snapshot=recovery_snapshot,
        include_disposition=True,
    )
    return recovery


def _recovery_source_binding(store, contract: Contract, source_record) -> dict | None:
    """Select the shared exact claim proof for a typed recovery source fact."""
    payload = source_record.payload
    method = method_payload(contract)
    requires_proof = method.get("method") in {"tool", "parallel_tool_item"}
    if not requires_proof:
        return None
    source_kind = source_record.record_type
    claim_id = payload.get("claim_id")
    if source_kind == "projection.decided":
        candidate_id = payload.get("candidate_id")
        rejected = [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.rejected"
            )
            if record.payload.get("candidate_id") == candidate_id
            and record.payload.get("contract_id") == contract.id
        ]
        claim_ids = {record.payload.get("claim_id") for record in rejected}
        claim_ids.discard(None)
        if not claim_ids:
            from aigineering.task_diagnostics import TaskDiagnosticError

            raise TaskDiagnosticError("recovery_projection_claim_missing")
        if len(claim_ids) != 1:
            from aigineering.task_diagnostics import TaskDiagnosticError

            raise TaskDiagnosticError("recovery_projection_claim_ambiguous")
        claim_id = next(iter(claim_ids))
    if not isinstance(claim_id, str) or not claim_id:
        from aigineering.task_diagnostics import TaskDiagnosticError

        raise TaskDiagnosticError("recovery_source_claim_missing")
    try:
        from aigineering.task_diagnostics import (
            TaskDiagnosticError,
            task_recovery_source_binding,
        )

        return task_recovery_source_binding(
            store, contract.id, claim_id, source_record.id, source_kind
        )
    except TaskDiagnosticError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskDiagnosticError("recovery_source_integrity_error") from exc


def _commit_recovery_outcome(
    store,
    contract: Contract,
    recovery,
    *,
    source_record,
    record_prefix: str,
    source_field: str,
    record_terminal: bool,
) -> None:
    from aigineering.plugins.recovery import RecoveryDisposition
    from aigineering.plugins.task_semantics import method_payload

    disposition = (
        recovery
        if isinstance(recovery, RecoveryDisposition)
        else RecoveryDisposition(recovery)
    )
    recovery_contract = disposition.contract
    method_fields = method_payload(contract)
    include_method_details = method_fields.get("method") in {
        "tool",
        "parallel_tool_item",
    }
    entry = create_entry(
        contract.id,
        "failed",
        relation_type="projection",
        relation_target=(
            recovery_contract.id
            if recovery_contract is not None
            else disposition.recovery_contract_id or "unrecoverable"
        ),
        authority_result="rejected",
        budget_remaining=contract.budget,
    )
    records = []
    if record_terminal:
        records.append(
            create_terminal_record(
                contract.id,
                "failed",
                causal_parents=[source_record.id],
            )
        )
    records.extend(
        (
            trace_record(entry, causal_parents=[source_record.id]),
            create_runtime_record(
                f"{record_prefix}.recovery_"
                f"{'scheduled' if recovery_contract is not None else 'unavailable'}",
                {
                    "contract_id": contract.id,
                    source_field: source_record.id,
                    **(
                        {"reason": disposition.reason}
                        if include_method_details and disposition.reason
                        else {}
                    ),
                    **(
                        {"recovery_contract_id": recovery_contract.id}
                        if include_method_details and recovery_contract is not None
                        else {}
                    ),
                    **(
                        {"recovery_contract_id": disposition.recovery_contract_id}
                        if include_method_details and disposition.recovery_contract_id
                        else {}
                    ),
                },
                causal_parents=[source_record.id],
            ),
        )
    )
    store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=[entry],
        runtime_records=tuple(records),
    )


def _commit_terminal_preserved_outcome(
    store,
    *,
    contract_id: str,
    terminal: str,
    source_record,
    record_prefix: str,
    source_field: str,
) -> None:
    """Record that terminal work cannot be recovered without changing it."""
    store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=[],
        runtime_records=(
            create_runtime_record(
                f"{record_prefix}.recovery_unavailable",
                {
                    "contract_id": contract_id,
                    source_field: source_record.id,
                    "reason": "terminal_preserved",
                    "terminal": terminal,
                },
                causal_parents=(source_record.id,),
            ),
        ),
    )


def process_rejected_submissions(store, *, candidate_publishers=None) -> list[str]:
    """Replay recovery from every durable claim-bound Candidate rejection."""
    processed: list[str] = []
    records = [record for _, record in store.scan_runtime_records()]
    candidates = {
        record.payload["candidate_id"]: record
        for record in records
        if record.record_type in {"candidate.received", "worker.output.received"}
        and "raw_output" in record.payload
    }
    terminal_contracts = {
        str(record.payload["contract_id"]): str(record.payload["terminal"])
        for record in records
        if record.record_type == "lifecycle.terminal"
    }
    recovered_source_ids = {
        str(
            record.payload.get("projection_id")
            or record.payload.get("rejection_id")
            or ""
        )
        for record in records
        if record.record_type
        in {
            "projection_rejection.recovery_scheduled",
            "projection_rejection.recovery_unavailable",
            "candidate_rejection.recovery_scheduled",
            "candidate_rejection.recovery_unavailable",
        }
    }
    for record in records:
        is_projection_rejection = (
            record.record_type == "projection.decided"
            and record.payload.get("status") == "rejected"
        )
        is_candidate_rejection = record.record_type == "candidate.rejected" and bool(
            record.payload.get("contract_id")
        )
        if not is_projection_rejection and not is_candidate_rejection:
            continue
        payload = record.payload
        contract_id = str(payload["contract_id"])
        if record.id in recovered_source_ids:
            continue
        contract = store.get_contract(contract_id)
        if contract is None:
            raise RuntimeError(
                f"rejected projection {record.id!r} references missing "
                f"Contract {contract_id!r}"
            )
        candidate_id = str(payload["candidate_id"])
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise RuntimeError(
                f"rejected projection {record.id!r} has no replayable raw "
                f"Candidate evidence for {candidate_id!r}"
            )
        source_binding = _recovery_source_binding(store, contract, record)
        if (
            method_payload(contract).get("method") in {"tool", "parallel_tool_item"}
            and source_binding is None
        ):
            from aigineering.task_diagnostics import TaskDiagnosticError

            raise TaskDiagnosticError("recovery_source_binding_missing")
        terminal = terminal_contracts.get(contract_id)
        if terminal is not None and terminal != "failed":
            _commit_terminal_preserved_outcome(
                store,
                contract_id=contract_id,
                terminal=terminal,
                source_record=record,
                record_prefix=(
                    "projection_rejection"
                    if is_projection_rejection
                    else "candidate_rejection"
                ),
                source_field=(
                    "projection_id" if is_projection_rejection else "rejection_id"
                ),
            )
            recovered_source_ids.add(record.id)
            processed.append(contract_id)
            continue
        rejections = (
            [dict(rejection) for rejection in payload["rejections"]]
            if is_projection_rejection
            else [
                {
                    "category": "candidate_rejection",
                    "name": "(candidate)",
                    "reject_reason": str(payload.get("reason", "Candidate rejected")),
                }
            ]
        )
        recovery = _schedule_rejected_recovery(
            contract,
            str(candidate.payload["raw_output"]),
            rejections,
            store,
            store,
            candidate_publishers=candidate_publishers,
            source_binding=source_binding,
        )
        _commit_recovery_outcome(
            store,
            contract,
            recovery,
            source_record=record,
            record_prefix=(
                "projection_rejection"
                if is_projection_rejection
                else "candidate_rejection"
            ),
            source_field="projection_id" if is_projection_rejection else "rejection_id",
            record_terminal=terminal is None,
        )
        recovered_source_ids.add(record.id)
        terminal_contracts[contract_id] = "failed"
        processed.append(contract_id)
    return processed


def process_expired_claims(store, *, candidate_publishers=None) -> list[str]:
    """Materialize expired leases and replay their recovery subtasks.

    Claim leases are sufficient evidence; runtime heartbeat ownership is not
    required.  The original contract is terminal and recovery always creates
    a new contract, preserving the no-reclaim invariant.
    """
    operational = require_operational_store(store)
    observed = datetime.now(timezone.utc)
    observed_at = observed.isoformat()
    records = [record for _, record in store.scan_runtime_records()]
    grant_ids = {
        str(record.payload["claim_id"]): record.id
        for record in records
        if record.record_type == "claim.granted"
    }

    for contract in store.get_all_contracts():
        claim = store.get_claim(contract.id)
        if claim is None or claim.get("status") != "active":
            continue
        lease_until = str(claim.get("lease_until", ""))
        malformed = False
        try:
            deadline = datetime.fromisoformat(lease_until)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            expired = deadline < observed
        except (TypeError, ValueError):
            malformed = True
            expired = True
        if not expired:
            continue
        grant_id = grant_ids.get(str(claim["claim_id"]))
        if grant_id is None:
            raise RuntimeError(
                f"claim {claim['claim_id']!r} has no immutable grant fact"
            )
        entry = create_entry(
            contract.id,
            "claim_expired",
            worker_id=str(claim["worker_id"]),
            rejected_fragments=[
                "[claim_expired] worker lease was malformed"
                if malformed
                else f"[claim_expired] lease ended at {lease_until}"
            ],
            authority_result="rejected",
        )
        expiration = create_runtime_record(
            "claim.expired",
            {
                "claim_id": claim["claim_id"],
                "contract_id": contract.id,
                "epoch": claim["epoch"],
                "lease_until": lease_until,
                "package_id": claim["package_id"],
                "worker_id": claim["worker_id"],
            },
            causal_parents=[grant_id],
            recorded_at=observed_at,
        )
        terminal = create_terminal_record(
            contract.id,
            "failed",
            causal_parents=[expiration.id],
            recorded_at=observed_at,
        )
        trace_fact = trace_record(
            entry, causal_parents=[expiration.id], recorded_at=observed_at
        )
        operational.commit_claim_expiration(
            trace_entry=entry,
            runtime_records=(expiration, terminal, trace_fact),
            claim_id=str(claim["claim_id"]),
            claim_epoch=int(claim["epoch"]),
            expected_lease_until=lease_until,
            observed_at=observed_at,
        )

    records = [record for _, record in store.scan_runtime_records()]
    processed_expiration_ids = {
        str(record.payload["expiration_id"])
        for record in records
        if record.record_type
        in {
            "claim_expiration.recovery_scheduled",
            "claim_expiration.recovery_unavailable",
        }
    }
    processed: list[str] = []
    for expiration in records:
        if (
            expiration.record_type != "claim.expired"
            or expiration.id in processed_expiration_ids
        ):
            continue
        contract_id = str(expiration.payload["contract_id"])
        contract = store.get_contract(contract_id)
        if contract is None:
            raise RuntimeError(
                f"claim expiration {expiration.id!r} references missing "
                f"Contract {contract_id!r}"
            )
        source_binding = _recovery_source_binding(store, contract, expiration)
        if (
            method_payload(contract).get("method") in {"tool", "parallel_tool_item"}
            and source_binding is None
        ):
            from aigineering.task_diagnostics import TaskDiagnosticError

            raise TaskDiagnosticError("recovery_source_binding_missing")
        recovery = _schedule_rejected_recovery(
            contract,
            "worker claim expired before Candidate submission",
            [
                {
                    "category": "claim_expired",
                    "name": "(claim)",
                    "reject_reason": (
                        f"lease ended at {expiration.payload['lease_until']}"
                    ),
                }
            ],
            store,
            store,
            candidate_publishers=candidate_publishers,
            source_binding=source_binding,
        )
        _commit_recovery_outcome(
            store,
            contract,
            recovery,
            source_record=expiration,
            record_prefix="claim_expiration",
            source_field="expiration_id",
            record_terminal=False,
        )
        processed_expiration_ids.add(expiration.id)
        processed.append(contract_id)
    return processed


def _record_worker_invocation_failure(
    claimed: ClaimedPackage,
    store,
    *,
    status_code: int,
    retryable: bool,
    category: str,
    raw_output: str | None = None,
    diagnostic: str | None = None,
    usage_metadata: Mapping[str, object] | None = None,
) -> None:
    existing = store.get_by_contract(claimed.contract.id)
    parent_id = existing[-1].id if existing else None
    safe_usage = normalize_usage_metadata(usage_metadata)
    entry = create_entry(
        claimed.contract.id,
        "worker_invocation_failed",
        parent_id=parent_id,
        worker_id=claimed.worker_id,
        rejected_fragments=[f"[{category}] status={status_code} retryable={retryable}"],
        authority_result="rejected",
        budget_remaining=claimed.package.budget_remaining,
        usage_metadata=safe_usage,
    )
    payload = {
        "claim_id": claimed.package.claim_id,
        "contract_id": claimed.contract.id,
        "package_id": claimed.package.package_id,
        "category": category,
        "retryable": retryable,
        "status_code": status_code,
        "worker_id": claimed.worker_id,
    }
    if raw_output is not None:
        payload["raw_output"] = raw_output[:4000]
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic[:1000]
    if safe_usage is not None:
        payload["usage_metadata"] = safe_usage
    failure = create_runtime_record(
        "worker.invocation_failed",
        payload,
    )
    terminal = create_terminal_record(
        claimed.contract.id,
        "failed",
        causal_parents=[failure.id],
    )
    trace_fact = trace_record(entry, causal_parents=[failure.id])
    require_operational_store(store).commit_worker_invocation_failure(
        trace_entry=entry,
        runtime_records=(failure, terminal, trace_fact),
        claim_id=claimed.package.claim_id,
        worker_id=claimed.worker_id,
        package_id=claimed.package.package_id,
        claim_epoch=claimed.package.claim_epoch,
    )


def process_worker_failures(store, *, candidate_publishers=None) -> list[str]:
    """Replay recovery scheduling for durably failed provider invocations."""
    records = [record for _, record in store.scan_runtime_records()]
    processed_failure_ids = {
        str(record.payload["failure_id"])
        for record in records
        if record.record_type
        in {
            "worker_failure.recovery_scheduled",
            "worker_failure.recovery_unavailable",
        }
    }
    processed: list[str] = []
    for failure in records:
        if (
            failure.record_type != "worker.invocation_failed"
            or failure.id in processed_failure_ids
        ):
            continue
        contract_id = str(failure.payload["contract_id"])
        contract = store.get_contract(contract_id)
        if contract is None:
            raise RuntimeError(
                f"worker failure {failure.id!r} references missing "
                f"Contract {contract_id!r}"
            )
        recovery = _schedule_rejected_recovery(
            contract,
            str(
                failure.payload.get(
                    "raw_output",
                    "provider invocation failed before Candidate production",
                )
            ),
            [
                {
                    "category": str(failure.payload["category"]),
                    "name": "(provider)",
                    "reject_reason": (
                        str(failure.payload["diagnostic"])
                        if "diagnostic" in failure.payload
                        else (
                            f"status={failure.payload['status_code']} "
                            f"retryable={failure.payload['retryable']}"
                        )
                    ),
                }
            ],
            store,
            store,
            candidate_publishers=candidate_publishers,
            source_binding=_recovery_source_binding(store, contract, failure),
        )
        _commit_recovery_outcome(
            store,
            contract,
            recovery,
            source_record=failure,
            record_prefix="worker_failure",
            source_field="failure_id",
            record_terminal=False,
        )
        processed_failure_ids.add(failure.id)
        processed.append(contract_id)
    return processed


def process_task_completions(
    store, completion_registry, *, candidate_publishers=None
) -> list[str]:
    """Project completed plugin tasks into their deterministic effects."""
    processed: list[str] = []
    projector = TaskCompletionProjector(
        store,
        completion_registry,
        candidate_publishers=candidate_publishers,
    )
    projected = {
        str(record.payload["source_contract_id"])
        for _, record in store.scan_runtime_records(
            record_type="task_completion.projected"
        )
    }
    for contract in store.get_all_contracts():
        if contract.parent_id is None or contract.origin != "system":
            continue
        entries = store.get_by_contract(contract.id)
        if not any(entry.event_type == "complete" for entry in entries):
            continue
        if contract.id in projected:
            continue
        if not projector.project(contract):
            continue
        marker = create_entry(
            contract_id=contract.id,
            event_type="task_completion_projected",
            relation_type="task_completion",
            relation_target=contract.parent_id,
        )
        marker_record = trace_record(marker)
        projected_record = create_runtime_record(
            "task_completion.projected",
            {
                "source_contract_id": contract.id,
                "parent_contract_id": contract.parent_id,
            },
        )
        store.commit_ingress_batch(
            accepted_assets=[],
            trace_entries=[marker],
            runtime_records=(marker_record, projected_record),
        )
        projected.add(contract.id)
        processed.append(contract.id)
    return processed


def _method_context_assets_for(contract: Contract, store) -> tuple[Asset, ...]:
    return method_context_assets_for(contract, store)
