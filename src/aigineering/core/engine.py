"""ACM boundary loop engine."""

from __future__ import annotations

from typing import Any

from aigineering.core.activation import check_activation
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.projection import project_candidate
from aigineering.core.store import MemoryStore
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Candidate, Contract


class Engine:
    def __init__(
        self,
        store: MemoryStore,
        worker: Any,
        trace_store: TraceStore | None = None,
    ) -> None:
        self._store = store
        self._worker = worker
        self._trace = trace_store if trace_store is not None else TraceStore()
        self._budget: dict[str, int] = {}
        self._completed: set[str] = set()

    def add_contract(self, contract: Contract) -> None:
        self._store.add_contract(contract)
        self._budget[contract.id] = contract.budget

    def add_asset(self, asset: Asset) -> None:
        self._store.add_asset(asset)

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
                and check_activation(c.activation, available_names)
            ]

            if not enabled:
                break

            for contract in enabled:
                self._trace.new_entry(
                    contract.id,
                    "activation",
                    budget_remaining=self._resolve_budget(contract),
                )

                scope = compute_disclosure(contract, self._store)
                self._trace.new_entry(
                    contract.id,
                    "disclosure",
                    disclosed_assets=[a.id for a in scope],
                    budget_remaining=self._resolve_budget(contract),
                )

                candidate: Candidate = self._worker.invoke(contract, scope)

                accepted, rejected = project_candidate(
                    contract, candidate, self._store
                )

                self._trace.new_entry(
                    contract.id,
                    "projection",
                    disclosed_assets=[a.id for a in scope],
                    worker_id=candidate.worker_id,
                    candidate_raw=candidate.raw_output,
                    accepted_fragments=[a.id for a in accepted],
                    rejected_fragments=[r["name"] for r in rejected],
                    authority_result=len(rejected) == 0,
                    budget_remaining=self._resolve_budget(contract),
                )

                remaining = self._resolve_budget(contract)
                self._budget[contract.id] = max(0, remaining - 1)

                if self._all_outputs_satisfied(contract):
                    self._trace.new_entry(
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

    def _all_outputs_satisfied(self, contract: Contract) -> bool:
        for output_name in contract.outputs:
            if not self._store.has_asset_named(output_name):
                return False
        return True
