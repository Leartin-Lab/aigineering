"""Shared worker execution helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.agent.llm import LLMWorker
from aigineering.agent.mock import MockWorker
from aigineering.core.activation import check_activation
from aigineering.core.disclosure import (
    DisclosurePolicyError,
    compute_disclosure,
    redact_for_disclosure,
)
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.submit import _all_outputs_satisfied, submit_candidate
from aigineering.core.worker_routing import is_eligible
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict, contract_to_dict

from aigineering.cli.task_state import project_task_status


@dataclass(frozen=True)
class ClaimedPackage:
    contract: Contract
    disclosed_assets: tuple[Asset, ...]
    package: WorkerPackage
    worker_id: str


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
    available_names = {a.name for a in store.get_all_assets()}
    get_registration = getattr(store, "get_worker_registration", None)
    registered_worker = get_registration(worker_id) if get_registration else None
    policy_blockers: list[DisclosurePolicyError] = []
    for contract in store.get_all_contracts():
        if contract_id is not None and contract.id != contract_id:
            continue
        if contract.activation and not check_activation(
            contract.activation, available_names
        ):
            continue
        status = project_task_status(contract, store)
        if status["terminal"] or status["status"] in {"waiting", "submitted"}:
            continue
        if _all_outputs_satisfied(contract, store):
            continue

        trace_entries = getattr(store, "get_by_contract", lambda _cid: [])(contract.id)
        budget_consumed = sum(
            1 for entry in trace_entries if entry.event_type == "budget_consumed"
        )
        remaining_budget = contract.budget - budget_consumed
        if remaining_budget <= 0:
            continue

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
            new_entry = getattr(store, "new_entry", None)
            if new_entry is not None:
                new_entry(
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
        claim_contract = getattr(store, "claim_contract", None)
        if claim_contract is not None:
            claim = claim_contract(
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
            new_entry = getattr(store, "new_entry", None)
            if new_entry is not None:
                new_entry(
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
) -> dict:
    """Invoke a worker and submit its candidate envelope."""
    trace = trace_store if trace_store is not None else store
    candidate = worker.invoke(claimed.contract, list(claimed.disclosed_assets))
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
    ingress = RuntimeIngress(store, trace)
    return submit_candidate(
        envelope=envelope,
        store=store,
        trace_store=trace,
        ingress=ingress,
        idempotency_key=envelope.idempotency_key,
    )


def _method_context_assets_for(contract: Contract, store) -> tuple[dict, ...]:
    get_all = getattr(store, "get_all", None)
    if get_all is None:
        return ()
    assets: list[Asset] = []
    seen: set[str] = set()
    for entry in get_all():
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
