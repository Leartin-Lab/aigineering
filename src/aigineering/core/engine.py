"""ACM boundary loop engine."""

from __future__ import annotations

import json
import logging
from typing import Any

from aigineering.core.activation import check_activation
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.labels import Label, resolve_contract_labels
from aigineering.core.projection import project_candidate
from aigineering.core.provenance import sign_asset
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import MemoryTraceStore, TraceStoreProtocol
from aigineering.protocol.types import Asset, Candidate, Contract, ProjectionResult

_logger = logging.getLogger(__name__)

_MAX_ACTIVATION_TOKENS = 200


def _safe_check_activation(expression: str, available_names: set[str]) -> bool:
    if len(expression) > _MAX_ACTIVATION_TOKENS:
        _logger.warning("Activation expression too long (%d chars)", len(expression))
        return False
    try:
        return check_activation(expression, available_names)
    except (ValueError, RecursionError) as e:
        _logger.warning("Invalid activation expression: %s", e)
        return False


class Engine:
    def __init__(
        self,
        store: StoreProtocol,
        worker: Any,
        trace_store: TraceStoreProtocol | None = None,
        labels: dict[str, Label] | None = None,
    ) -> None:
        self._store = store
        self._worker = worker
        self._trace = trace_store if trace_store is not None else MemoryTraceStore()
        self._labels = labels if labels is not None else {}
        self._label_context: dict[str, list[Asset]] = {}
        self._budget: dict[str, int] = {}
        self._completed: set[str] = set()
        self._contract_last_entry: dict[str, str] = {}  # contract_id → last trace entry id

    def add_contract(self, contract: Contract) -> None:
        self._store.add_contract(contract)
        self._budget[contract.id] = max(contract.budget, 1)
        if contract.labels:
            resolution = resolve_contract_labels(contract, self._labels, self._store)
            self._label_context[contract.id] = resolution.injected_assets
            self._add_trace(
                contract.id,
                "label_resolved",
                disclosed_assets=[asset.id for asset in resolution.injected_assets],
                relation_type="label",
                relation_target=",".join(resolution.label_names),
                budget_remaining=self._resolve_budget(contract),
            )

    def add_asset(self, asset: Asset) -> None:
        self._store.add_asset(sign_asset(asset))

    def _add_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        parent_id = self._contract_last_entry.get(contract_id)
        entry = self._trace.new_entry(contract_id, event_type, parent_id=parent_id, **kwargs)
        self._contract_last_entry[contract_id] = entry.id

    def _commit(self, result: ProjectionResult) -> None:
        for asset in result.accepted_assets:
            self._store.add_asset(sign_asset(asset))

    def run(self) -> None:
        while True:
            available_names: set[str] = {
                a.name for a in self._store.get_all_assets()
            }

            enabled: list[Contract] = [
                c
                for c in self._store.get_all_contracts()
                if c.id not in self._completed
                and self._resolve_budget(c) > 0
                and _safe_check_activation(c.activation, available_names)
            ]

            if not enabled:
                break

            for contract in enabled:
                self._add_trace(
                    contract.id,
                    "activation",
                    budget_remaining=self._resolve_budget(contract),
                )

                scope = self._compute_scope(contract)
                self._add_trace(
                    contract.id,
                    "disclosure",
                    disclosed_assets=[a.id for a in scope],
                    budget_remaining=self._resolve_budget(contract),
                )

                candidate: Candidate = self._worker.invoke(contract, scope)

                result = project_candidate(contract, candidate)
                self._commit(result)
                accepted = result.accepted_assets
                rejected_dicts = [
                    {
                        "name": r.name,
                        "content": r.content,
                        "reject_reason": r.reject_reason,
                        "category": r.category.value,
                    }
                    for r in result.rejected_candidates
                ]

                self._add_trace(
                    contract.id,
                    "projection",
                    disclosed_assets=[a.id for a in scope],
                    worker_id=candidate.worker_id,
                    candidate_raw=candidate.raw_output,
                    accepted_fragments=[a.id for a in result.accepted_assets],
                    accepted_asset_names=[a.name for a in result.accepted_assets],
                    rejected_fragments=[
                        f"[{r['category']}] {r['name']}: {r['reject_reason']}"
                        for r in rejected_dicts
                    ],
                    authority_result=result.status.value,
                    authority_policy=(
                        json.dumps(result.authority_policy, sort_keys=True)
                        if result.authority_policy is not None
                        else None
                    ),
                    budget_remaining=self._resolve_budget(contract),
                )

                remaining = self._resolve_budget(contract)
                self._budget[contract.id] = max(0, remaining - 1)

                if self._all_outputs_satisfied(contract):
                    self._add_trace(
                        contract.id,
                        "complete",
                        budget_remaining=self._resolve_budget(contract),
                    )
                    self._completed.add(contract.id)
                    break

    def _resolve_budget(self, contract: Contract) -> int:
        if contract.id not in self._budget:
            self._budget[contract.id] = max(contract.budget, 1)
        return self._budget[contract.id]

    def _compute_scope(self, contract: Contract) -> list[Asset]:
        seen: set[str] = set()
        scope: list[Asset] = []
        for asset in compute_disclosure(contract, self._store):
            if asset.id not in seen:
                seen.add(asset.id)
                scope.append(asset)
        for asset in self._label_context.get(contract.id, []):
            if not asset.promptable:
                continue
            if asset.id not in seen:
                seen.add(asset.id)
                scope.append(asset)
        return scope

    def _all_outputs_satisfied(self, contract: Contract) -> bool:
        for output_name in contract.outputs:
            matching = self._store.get_assets_by_name(output_name)
            if not any(a.created_by == contract.id for a in matching):
                return False
        return True
