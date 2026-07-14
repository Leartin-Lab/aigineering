"""Shared worker execution helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from threading import Event, Thread

from aigineering.agent.llm import LLMWorker
from aigineering.agent.mock import MockWorker
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
from aigineering.core.ids import hash_retry
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.method_handlers.recovery import schedule_projection_recovery
from aigineering.core.methods import (
    method_context_content,
    method_contract,
    system_asset,
)
from aigineering.core.provenance import sign_asset
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.submit import (
    SubmitConflictError,
    submit_candidate,
)
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
    package: WorkerPackage
    worker_id: str


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


def build_worker(
    worker_kind: str,
    *,
    model: str | None = None,
    base_url: str = "https://api.openai.com/v1",
    timeout: float = 60.0,
    max_retries: int = 3,
    capabilities: frozenset[str] | None = None,
) -> MockWorker | LLMWorker:
    """Build a CLI worker implementation."""
    if worker_kind == "mock":
        return MockWorker()
    if worker_kind == "llm":
        if not model:
            raise ValueError("--model is required when --worker llm")
        return LLMWorker(
            model=model,
            base_url=base_url,
            timeout=int(timeout),
            max_retries=max_retries,
            capabilities=capabilities or frozenset(),
        )
    raise ValueError(f"unsupported worker: {worker_kind}")


def claim_next_package(
    store,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    contract_id: str | None = None,
) -> ClaimedPackage | None:
    """Claim the next ready contract and return its worker package."""
    store = require_operational_store(store)
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
            method_context_assets=method_context_assets,
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
        package = WorkerPackage(
            contract_id=contract.id,
            contract=contract_to_dict(contract),
            disclosed_assets=tuple(asset_to_dict(a) for a in disclosed),
            method_context_assets=method_context_assets,
            tool_scope=contract.tool_scope,
            budget_remaining=remaining_budget,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            lease_until=claim["lease_until"],
            package_id=package.package_id,
            capability_requirements=contract.worker_capabilities,
            worker_profile_id=(
                registered_worker.profile_id if registered_worker else ""
            ),
            worker_registration_version=(
                registered_worker.version if registered_worker else ""
            ),
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
        return ClaimedPackage(contract, disclosed, package, worker_id)
    if policy_blockers:
        reasons = [reason for exc in policy_blockers for reason in exc.reasons]
        raise DisclosurePolicyError(policy_blockers[0].contract_id, reasons)
    return None


def execute_claimed_package(
    claimed: ClaimedPackage,
    worker: MockWorker | LLMWorker,
    store,
    trace_store=None,
    method_registry=None,
) -> dict:
    """Invoke a worker and submit its candidate envelope."""
    trace = trace_store if trace_store is not None else store
    keeper = _ClaimLeaseKeeper(claimed, store)
    keeper.start()
    try:
        candidate = worker.invoke(claimed.contract, list(claimed.disclosed_assets))
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
    )
    return submit_candidate_envelope(
        envelope,
        store,
        trace_store=trace,
        method_registry=method_registry,
    )


def submit_candidate_envelope(
    envelope: CandidateEnvelope,
    store,
    *,
    trace_store=None,
    method_registry=None,
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
    )
    method_action = parse_method_action(candidate)
    if method_action is not None:
        if method_registry is None:
            raise ValueError(
                f"worker produced /{method_action.type} but no method registry is configured"
            )
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
            method_registry,
        )
    ingress = RuntimeIngress(store, trace)
    result = submit_candidate(
        envelope=envelope,
        store=store,
        trace_store=trace,
        ingress=ingress,
        idempotency_key=envelope.idempotency_key,
    )
    if result["status"] == "rejected":
        process_rejected_submissions(store)
    return result


def _schedule_rejected_recovery(
    contract: Contract,
    candidate_raw: str,
    rejections: list[dict],
    store,
    trace,
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
    store.append_runtime_record(
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": contract.id, "terminal": "failed"},
        )
    )


def process_rejected_submissions(store) -> list[str]:
    """Replay missing recovery effects from immutable rejected projections."""
    processed: list[str] = []
    records = [record for _, record in store.scan_runtime_records()]
    candidates = {
        record.payload["candidate_id"]: record
        for record in records
        if record.record_type == "candidate.received"
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
        candidate = candidates.get(payload["candidate_id"])
        if contract is None or candidate is None:
            continue
        _schedule_rejected_recovery(
            contract,
            str(candidate.payload["raw_output"]),
            [dict(rejection) for rejection in payload["rejections"]],
            store,
            store,
        )
        terminal_contracts.add(contract_id)
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
    method_registry,
) -> dict:
    """Atomically schedule a claim-bound method action."""
    if envelope.idempotency_key:
        cached = store.get_idempotency(contract.id, envelope.idempotency_key)
        if cached is not None:
            cached_response = dict(cached)
            cached_candidate_hash = cached_response.pop("_candidate_hash", "")
            if (
                cached_candidate_hash
                and cached_candidate_hash != envelope.candidate_hash
            ):
                raise SubmitConflictError(
                    f"Idempotency key for contract '{contract.id}' is already "
                    "bound to a different Candidate payload"
                )
            cached_response["duplicate"] = True
            return cached_response
        if store.has_any_idempotency(contract.id):
            raise SubmitConflictError(
                f"Contract '{contract.id}' already has a submission with a "
                "different idempotency key"
            )
    claim = store.get_claim(contract.id)
    if (
        claim is None
        or claim.get("claim_id") != envelope.claim_id
        or claim.get("epoch") != envelope.claim_epoch
        or claim.get("worker_id") != envelope.worker_id
        or claim.get("package_id") != envelope.package_id
        or claim.get("status") != "active"
    ):
        raise ValueError("method submission failed active claim/package fencing")

    handler = method_registry.get(action.type)
    if handler is None or not handler.can_handle(action.type):
        raise ValueError(f"no method handler registered for /{action.type}")

    if action.type == "retry":
        child = Contract(
            id=hash_retry(contract.id),
            parent_id=contract.parent_id,
            name=contract.name,
            description=contract.description,
            inputs=contract.inputs,
            outputs=contract.outputs,
            activation=contract.activation,
            budget=contract.budget,
            tool_scope=contract.tool_scope,
            labels=contract.labels,
            worker_capabilities=contract.worker_capabilities,
            worker_pools=contract.worker_pools,
            origin=contract.origin,
            sensitive_input_policy=contract.sensitive_input_policy,
        )
        context_asset = None
        event_type = "retry_created"
    else:
        child = method_contract(contract, action)
        context_asset = sign_asset(
            system_asset(
                name=f"_method_ctx_{contract.id}",
                content=method_context_content(contract, action, child),
                created_by=contract.id,
            )
        )
        event_type = "method_scheduled"

    existing = trace.get_by_contract(contract.id)
    parent_id = existing[-1].id if existing else None
    method_entry = create_entry(
        contract.id,
        event_type,
        parent_id=parent_id,
        worker_id=envelope.worker_id,
        candidate_raw=candidate.raw_output,
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
    candidate_record = create_runtime_record(
        "candidate.received",
        {
            "candidate_id": envelope.candidate_hash,
            "claim_epoch": envelope.claim_epoch,
            "claim_id": envelope.claim_id,
            "contract_id": contract.id,
            "method": action.type,
            "package_id": envelope.package_id,
            "raw_output": candidate.raw_output,
            "worker_id": envelope.worker_id,
        },
    )
    method_record = create_runtime_record(
        "method.scheduled",
        {
            "contract_id": contract.id,
            "method": action.type,
            "relation_target": child.id,
        },
        causal_parents=[candidate_record.id],
    )
    runtime_records = [
        candidate_record,
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
        idempotency_result={**response, "_candidate_hash": envelope.candidate_hash},
        claim_id=envelope.claim_id,
        worker_id=envelope.worker_id,
        package_id=envelope.package_id,
        claim_epoch=envelope.claim_epoch,
    )
    return response


def process_method_completions(store, method_registry) -> list[str]:
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
        method_registry=method_registry,
        completed=set(),
        suspended=set(),
        method_scheduled=set(),
        method_context={},
        ingress=RuntimeIngress(store, store, FactReducer(store, store)),
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


def _method_context_assets_for(contract: Contract, store) -> tuple[dict, ...]:
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
    return tuple(asset_to_dict(asset) for asset in assets)
