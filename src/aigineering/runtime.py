"""Application-neutral worker protocol execution and recovery services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import sqlite3
from threading import Event, Thread

from aigineering.agent.llm import LLMWorker, ProviderError
from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerExecutionError, WorkerHost
from aigineering.core.activation import check_activation
from aigineering.core.budget_manager import BudgetManager
from aigineering.core.continuation_manager import ContinuationManager
from aigineering.core.disclosure import (
    DisclosurePolicyError,
    compute_disclosure,
    redact_for_disclosure,
)
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.store import require_operational_store
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.method_handlers.recovery import schedule_projection_recovery
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.submit import (
    SubmitClaimError,
    SubmitCommitError,
    SubmitConflictError,
    WorkerCandidateAuthentication,
    authenticate_worker_candidate,
    replay_idempotent_submission,
    submit_authenticated_worker_candidate,
    submit_candidate,
    validate_submission_claim,
)
from aigineering.core.commitment import record_candidate_rejection
from aigineering.core.worker_routing import is_eligible
from aigineering.core.trace_manager import TraceManager
from aigineering.core.trace import create_entry
from aigineering.protocol.actions import parse_method_action
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Candidate, Contract
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_to_dict,
    trace_entry_to_dict,
)


@dataclass(frozen=True)
class ClaimedPackage:
    contract: Contract
    disclosed_assets: tuple[Asset, ...]
    method_context_assets: tuple[Asset, ...]
    package: WorkerPackage
    worker_id: str


class WorkerInvocationError(RuntimeError):
    """A claimed provider call failed and was durably closed."""


class _ClaimLeaseKeeper:
    """Renew one claim while its synchronous provider invocation is running."""

    def __init__(self, claimed: ClaimedPackage, store) -> None:
        self._claimed = claimed
        self._store = store
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
        if not self._claimed.package.claim_id:
            return
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._interval * 2))

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._store.renew_claim(
                    self._claimed.package.claim_id,
                    self._claimed.package.claim_epoch,
                    self._claimed.worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except (RuntimeError, sqlite3.Error):
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
    candidate_publishers=None,
) -> ClaimedPackage | None:
    """Claim the next ready contract and return its worker package."""
    store = require_operational_store(store)
    process_expired_claims(store, candidate_publishers=candidate_publishers)
    available_names = {a.name for a in store.get_all_assets()}
    registered_worker = store.get_worker_registration(worker_id)
    policy_blockers: list[DisclosurePolicyError] = []
    for contract in store.get_all_contracts():
        if contract_id is not None and contract.id != contract_id:
            continue
        if contract.activation and not check_activation(
            contract.activation, available_names
        ):
            continue
        view = RuntimeProjection(store, store).contract_view(contract)
        if not view.enabled:
            continue

        remaining_budget = view.budget_remaining

        # Compatibility remains available for legacy unconstrained contracts,
        # but a constrained contract is never claimed by an unknown or
        # ineligible worker. Routing labels are not disclosed prompt assets.
        if contract.worker_capabilities or contract.worker_pools:
            if registered_worker is None or not is_eligible(
                contract, registered_worker
            ):
                continue
        elif registered_worker is not None and not is_eligible(
            contract, registered_worker
        ):
            continue

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
        package = WorkerPackage(
            contract_id=contract.id,
            contract=contract_to_dict(contract),
            disclosed_assets=tuple(asset_to_dict(a) for a in disclosed),
            method_context_assets=tuple(
                asset_to_dict(asset) for asset in method_context_assets
            ),
            tool_scope=contract.tool_scope,
            budget_remaining=remaining_budget,
            capability_requirements=contract.worker_capabilities,
            worker_profile_id=(
                registered_worker.profile_id if registered_worker else ""
            ),
            worker_registration_version=(
                registered_worker.version if registered_worker else ""
            ),
        )
        claim = store.claim_contract(
            contract.id,
            worker_id,
            lease_seconds=lease_seconds,
            package_id=package.package_id,
            expected_registration_version=(
                registered_worker.version if registered_worker else ""
            ),
        )
        if claim is None:
            continue
        package = replace(
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
                f"{registered_worker.profile_id}@{registered_worker.version}"
                if registered_worker
                else "legacy"
            ),
            budget_remaining=remaining_budget,
        )
        return ClaimedPackage(
            contract, disclosed, method_context_assets, package, worker_id
        )
    if policy_blockers:
        reasons = [reason for exc in policy_blockers for reason in exc.reasons]
        raise DisclosurePolicyError(policy_blockers[0].contract_id, reasons)
    return None


def execute_claimed_package(
    claimed: ClaimedPackage,
    worker: MockWorker | LLMWorker | WorkerHost,
    store,
    trace_store=None,
    candidate_publishers=None,
) -> dict:
    """Invoke a worker and submit its candidate envelope."""
    trace = trace_store if trace_store is not None else store
    keeper = _ClaimLeaseKeeper(claimed, store)
    keeper.start()
    try:
        try:
            candidate = worker.invoke(
                claimed.contract,
                list(claimed.disclosed_assets + claimed.method_context_assets),
            )
        except (ProviderError, WorkerExecutionError) as exc:
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
            )
            process_worker_failures(store, candidate_publishers=candidate_publishers)
            raise WorkerInvocationError(
                f"worker invocation failed with status {status_code}; "
                "claim was released and recovery was scheduled"
            ) from None
    finally:
        keeper.stop()
    if keeper.failed:
        raise ValueError(
            f"claim lease renewal failed for {claimed.package.claim_id!r}; "
            "worker result was not submitted"
        )
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
        usage_metadata=candidate.metadata,
    )
    if isinstance(worker, WorkerHost):
        return submit_worker_proposal(
            worker.sign_envelope(envelope),
            store,
            trace_store=trace,
            candidate_publishers=candidate_publishers,
        )
    return submit_candidate_envelope(
        envelope,
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
    """Submit one WorkerHost-signed proposal, including transitional methods."""
    trace = trace_store if trace_store is not None else store
    envelope, authentication = authenticate_worker_candidate(proposal, store, trace)
    contract = store.get_contract(envelope.contract_id)
    if contract is None:
        raise ValueError(f"Contract '{envelope.contract_id}' not found in store")
    candidate = Candidate(
        worker_id=envelope.worker_id,
        raw_output=envelope.raw_output,
        parsed_action=envelope.parsed_action,
        metadata=envelope.usage_metadata,
    )
    method_action = parse_method_action(candidate)
    if method_action is None:
        result = submit_authenticated_worker_candidate(
            envelope, authentication, store, trace
        )
        if result["status"] == "rejected":
            process_rejected_submissions(
                store, candidate_publishers=candidate_publishers
            )
        return result
    try:
        budget_consumed = sum(
            1
            for entry in trace.get_by_contract(contract.id)
            if entry.event_type == "budget_consumed"
        )
        return _submit_claimed_method(
            contract,
            envelope,
            candidate,
            method_action,
            max(0, contract.budget - budget_consumed),
            store,
            trace,
            authentication=authentication,
        )
    except (
        DisclosurePolicyError,
        SubmitClaimError,
        SubmitCommitError,
        SubmitConflictError,
        TypeError,
        ValueError,
    ) as exc:
        record_candidate_rejection(
            proposal,
            str(exc),
            store,
            trace,
            receipt=authentication.receipt,
        )
        raise


def submit_candidate_envelope(
    envelope: CandidateEnvelope,
    store,
    *,
    trace_store=None,
    candidate_publishers=None,
) -> dict:
    """Submit one already-produced Candidate through the shared protocol."""
    trace = trace_store if trace_store is not None else store
    contract = store.get_contract(envelope.contract_id)
    if contract is None:
        raise ValueError(f"Contract '{envelope.contract_id}' not found in store")
    candidate = Candidate(
        worker_id=envelope.worker_id,
        raw_output=envelope.raw_output,
        parsed_action=envelope.parsed_action,
        metadata=envelope.usage_metadata,
    )
    method_action = parse_method_action(candidate)
    if method_action is not None:
        budget_consumed = sum(
            1
            for entry in trace.get_by_contract(contract.id)
            if entry.event_type == "budget_consumed"
        )
        return _submit_claimed_method(
            contract,
            envelope,
            candidate,
            method_action,
            max(0, contract.budget - budget_consumed),
            store,
            trace,
        )
    result = submit_candidate(
        envelope=envelope,
        store=store,
        trace_store=trace,
        idempotency_key=envelope.idempotency_key,
    )
    if result["status"] == "rejected":
        process_rejected_submissions(store, candidate_publishers=candidate_publishers)
    return result


def _schedule_rejected_recovery(
    contract: Contract,
    candidate_raw: str,
    rejections: list[dict],
    store,
    trace,
    *,
    record_terminal: bool = True,
    candidate_publishers=None,
) -> None:
    budget = BudgetManager()
    for current in store.get_all_contracts():
        budget.initialize(current.id, current.budget)
    trace_manager = TraceManager(trace)
    runtime = MethodRuntime(
        store=store,
        trace=trace_manager,
        budget=budget,
        ingress=RuntimeIngress(store, trace, FactReducer(store, trace)),
        candidate_publishers=candidate_publishers,
    )
    recovery = schedule_projection_recovery(
        runtime,
        failed_contract=contract,
        candidate_raw=candidate_raw,
        rejections=rejections,
    )
    trace_manager.record(
        contract.id,
        "failed",
        relation_type="projection",
        relation_target=recovery.id if recovery is not None else "unrecoverable",
        authority_result="rejected",
        budget_remaining=budget.get_remaining(contract.id),
    )
    if record_terminal:
        store.append_runtime_record(
            create_runtime_record(
                "lifecycle.terminal",
                {"contract_id": contract.id, "terminal": "failed"},
            )
        )


def process_rejected_submissions(store, *, candidate_publishers=None) -> list[str]:
    """Replay missing recovery effects from immutable rejected projections."""
    processed: list[str] = []
    records = [record for _, record in store.scan_runtime_records()]
    candidates = {
        record.payload["candidate_id"]: record
        for record in records
        if record.record_type in {"candidate.received", "worker.output.received"}
        and "raw_output" in record.payload
    }
    terminal_contracts = {
        record.payload["contract_id"]
        for record in records
        if record.record_type == "lifecycle.terminal"
    }
    for record in records:
        if record.record_type != "projection.decided":
            continue
        payload = record.payload
        contract_id = str(payload["contract_id"])
        if payload["status"] != "rejected" or contract_id in terminal_contracts:
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
        _schedule_rejected_recovery(
            contract,
            str(candidate.payload["raw_output"]),
            [dict(rejection) for rejection in payload["rejections"]],
            store,
            store,
            candidate_publishers=candidate_publishers,
        )
        terminal_contracts.add(contract_id)
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
        terminal = create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": contract.id, "terminal": "failed"},
            causal_parents=[expiration.id],
            recorded_at=observed_at,
        )
        trace_record = create_runtime_record(
            "trace.recorded",
            {"trace": trace_entry_to_dict(entry)},
            causal_parents=[expiration.id],
            recorded_at=observed_at,
        )
        operational.commit_claim_expiration(
            trace_entry=entry,
            runtime_records=(expiration, terminal, trace_record),
            claim_id=str(claim["claim_id"]),
            claim_epoch=int(claim["epoch"]),
            expected_lease_until=lease_until,
            observed_at=observed_at,
        )

    records = [record for _, record in store.scan_runtime_records()]
    processed_expiration_ids = {
        str(record.payload["expiration_id"])
        for record in records
        if record.record_type == "claim_expiration.recovery_scheduled"
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
        _schedule_rejected_recovery(
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
            record_terminal=False,
            candidate_publishers=candidate_publishers,
        )
        marker = create_runtime_record(
            "claim_expiration.recovery_scheduled",
            {"contract_id": contract_id, "expiration_id": expiration.id},
            causal_parents=[expiration.id],
        )
        store.append_runtime_record(marker)
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
) -> None:
    existing = store.get_by_contract(claimed.contract.id)
    parent_id = existing[-1].id if existing else None
    entry = create_entry(
        claimed.contract.id,
        "worker_invocation_failed",
        parent_id=parent_id,
        worker_id=claimed.worker_id,
        rejected_fragments=[f"[{category}] status={status_code} retryable={retryable}"],
        authority_result="rejected",
        budget_remaining=claimed.package.budget_remaining,
    )
    failure = create_runtime_record(
        "worker.invocation_failed",
        {
            "claim_id": claimed.package.claim_id,
            "contract_id": claimed.contract.id,
            "package_id": claimed.package.package_id,
            "category": category,
            "retryable": retryable,
            "status_code": status_code,
            "worker_id": claimed.worker_id,
        },
    )
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": claimed.contract.id, "terminal": "failed"},
        causal_parents=[failure.id],
    )
    trace_record = create_runtime_record(
        "trace.recorded",
        {"trace": trace_entry_to_dict(entry)},
        causal_parents=[failure.id],
    )
    require_operational_store(store).commit_worker_invocation_failure(
        trace_entry=entry,
        runtime_records=(failure, terminal, trace_record),
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
        if record.record_type == "worker_failure.recovery_scheduled"
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
        _schedule_rejected_recovery(
            contract,
            "provider invocation failed before Candidate production",
            [
                {
                    "category": str(failure.payload["category"]),
                    "name": "(provider)",
                    "reject_reason": (
                        f"status={failure.payload['status_code']} "
                        f"retryable={failure.payload['retryable']}"
                    ),
                }
            ],
            store,
            store,
            record_terminal=False,
            candidate_publishers=candidate_publishers,
        )
        marker = create_runtime_record(
            "worker_failure.recovery_scheduled",
            {"contract_id": contract_id, "failure_id": failure.id},
            causal_parents=[failure.id],
        )
        store.append_runtime_record(marker)
        processed_failure_ids.add(failure.id)
        processed.append(contract_id)
    return processed


def _submit_claimed_method(
    contract: Contract,
    envelope: CandidateEnvelope,
    candidate,
    action,
    budget_remaining: int,
    store,
    trace,
    *,
    authentication: WorkerCandidateAuthentication | None = None,
) -> dict:
    """Atomically schedule a claim-bound method action."""
    candidate_id = (
        authentication.candidate_id if authentication else envelope.candidate_hash
    )
    if (
        authentication is not None
        and authentication.candidate.effects[0].effect_type != "task.delegate"
    ):
        raise ValueError("signed method submission requires a 'task.delegate' effect")
    duplicate = replay_idempotent_submission(
        store,
        contract_id=contract.id,
        idempotency_key=envelope.idempotency_key,
        candidate_hash=candidate_id,
    )
    if duplicate is not None:
        return duplicate
    validate_submission_claim(store, contract, envelope)

    from aigineering.plugins import TaskDelegationPlugin

    delegation = TaskDelegationPlugin().project(contract, action)
    child = delegation.child
    context_asset = delegation.context_asset
    event_type = delegation.event_type

    existing = trace.get_by_contract(contract.id)
    parent_id = existing[-1].id if existing else None
    method_entry = create_entry(
        contract.id,
        event_type,
        parent_id=parent_id,
        worker_id=envelope.worker_id,
        candidate_raw=candidate.raw_output,
        usage_metadata=envelope.usage_metadata,
        relation_type=action.type,
        relation_target=child.id,
        disclosed_assets=[context_asset.id] if context_asset is not None else [],
        budget_remaining=budget_remaining,
    )
    remaining = max(0, budget_remaining - 1)
    budget_entry = create_entry(
        contract.id,
        "budget_consumed",
        parent_id=method_entry.id,
        relation_type=action.type,
        budget_remaining=remaining,
    )
    candidate_record = (
        authentication.receipt
        if authentication
        else create_runtime_record(
            "candidate.received",
            {
                "candidate_id": candidate_id,
                "claim_epoch": envelope.claim_epoch,
                "claim_id": envelope.claim_id,
                "contract_id": contract.id,
                "method": action.type,
                "package_id": envelope.package_id,
                "raw_output": candidate.raw_output,
                "worker_id": envelope.worker_id,
                "usage_metadata": envelope.usage_metadata,
            },
        )
    )
    output_record = None
    method_parent_id = candidate_record.id
    if authentication is not None:
        output_record = create_runtime_record(
            "worker.delegation.received",
            {
                "candidate_id": candidate_id,
                "claim_epoch": envelope.claim_epoch,
                "claim_id": envelope.claim_id,
                "contract_id": contract.id,
                "idempotency_key": envelope.idempotency_key,
                "method": action.type,
                "package_id": envelope.package_id,
                "raw_output": candidate.raw_output,
                "usage_metadata": envelope.usage_metadata,
                "worker_id": envelope.worker_id,
            },
            causal_parents=[candidate_record.id],
        )
        method_parent_id = output_record.id
    method_record = create_runtime_record(
        "method.scheduled",
        {
            "contract_id": contract.id,
            "method": action.type,
            "relation_target": child.id,
        },
        causal_parents=[method_parent_id],
    )
    runtime_records = [candidate_record]
    if output_record is not None:
        runtime_records.append(output_record)
    runtime_records.extend(
        [
            method_record,
            create_runtime_record(
                "budget.consumed",
                {
                    "amount": 1,
                    "contract_id": contract.id,
                    "remaining": remaining,
                    "trace_id": budget_entry.id,
                },
                causal_parents=[method_record.id],
            ),
            create_runtime_record(
                "contract.declared",
                {"contract": contract_to_dict(child)},
                causal_parents=[method_record.id],
            ),
            create_runtime_record(
                "trace.recorded",
                {"trace": trace_entry_to_dict(method_entry)},
                causal_parents=[method_record.id],
            ),
            create_runtime_record(
                "trace.recorded",
                {"trace": trace_entry_to_dict(budget_entry)},
                causal_parents=[method_record.id],
            ),
        ]
    )
    if context_asset is not None:
        runtime_records.append(
            create_runtime_record(
                "asset.committed",
                {
                    "asset": asset_to_dict(context_asset),
                    "contract_id": context_asset.created_by,
                },
                causal_parents=[method_record.id],
            )
        )
    response = {
        "contract_id": contract.id,
        "status": "method_scheduled",
        "method": action.type,
        "child_contract_id": child.id,
        "complete": False,
        "duplicate": False,
    }
    operational_store = require_operational_store(store)
    operational_store.commit_method_submission(
        child_contract=child,
        context_asset=context_asset,
        trace_entries=[method_entry, budget_entry],
        runtime_records=tuple(runtime_records),
        idempotency_key=envelope.idempotency_key,
        idempotency_result={**response, "_candidate_hash": candidate_id},
        claim_id=envelope.claim_id,
        worker_id=envelope.worker_id,
        package_id=envelope.package_id,
        claim_epoch=envelope.claim_epoch,
        candidate_key_id=authentication.key_id if authentication else "",
    )
    return response


def process_method_completions(
    store, completion_registry, *, candidate_publishers=None
) -> list[str]:
    """Project completed method Contracts into their deterministic effects."""
    processed: list[str] = []
    trace_manager = TraceManager(store)
    budget = BudgetManager()
    for contract in store.get_all_contracts():
        budget.initialize(contract.id, contract.budget)
    continuations = ContinuationManager(
        store=store,
        budget_mgr=budget,
        trace_mgr=trace_manager,
        completion_registry=completion_registry,
        completed=set(),
        suspended=set(),
        method_scheduled=set(),
        method_context={},
        candidate_publishers=candidate_publishers,
    )
    for contract in store.get_all_contracts():
        if contract.origin != "system" or contract.parent_id is None:
            continue
        entries = store.get_by_contract(contract.id)
        if not any(entry.event_type == "complete" for entry in entries):
            continue
        if any(entry.event_type == "method_processed" for entry in entries):
            continue
        continuations.resume_parent_from_method(contract)
        trace_manager.record(
            contract.id,
            "method_processed",
            relation_type="method_completion",
            relation_target=contract.parent_id,
        )
        processed.append(contract.id)
    return processed


def _method_context_assets_for(contract: Contract, store) -> tuple[Asset, ...]:
    store = require_operational_store(store)
    assets: list[Asset] = []
    seen: set[str] = set()
    for entry in store.get_all():
        if (
            entry.event_type != "method_continuation_scheduled"
            or entry.relation_target != contract.id
        ):
            continue
        for asset_id in entry.disclosed_assets:
            asset = store.get_asset(asset_id)
            if asset is None or asset.id in seen:
                continue
            assets.append(redact_for_disclosure(asset))
            seen.add(asset.id)
    return tuple(assets)
