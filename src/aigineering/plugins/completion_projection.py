"""Stateless projection of completed delegated tasks.

The production runtime reconstructs completion work from immutable Contracts,
Assets and RuntimeRecords on every pass.  It deliberately owns no suspended,
resumed, or method-scheduled process state.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

from aigineering.core.budget_manager import BudgetManager
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.runtime_projection import TERMINAL_EVENTS
from aigineering.core.trace import create_entry
from aigineering.core.trace_manager import TraceManager
from aigineering.core.trace import TraceStoreProtocol
from aigineering.core.store import StoreProtocol
from aigineering.plugins.continuation import ContinuationTaskPlugin
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import contract_from_dict, trace_entry_to_dict

if TYPE_CHECKING:
    from aigineering.core.candidate_publisher import CandidatePublisherRegistry
    from aigineering.core.commitment import CommitmentDecision
    from aigineering.plugins.completion import CompletionRegistry
    from aigineering.protocol.candidate import CandidateEffect
    from aigineering.protocol.types import Asset, Contract


class CompletionStoreProtocol(StoreProtocol, TraceStoreProtocol, Protocol):
    """Store surface required by stateless completion projection."""


class TaskCompletionContext:
    """Narrow Candidate-only context supplied to completion plugins."""

    def __init__(
        self,
        store: CompletionStoreProtocol,
        trace: TraceManager,
        budget: BudgetManager,
        candidate_publishers: CandidatePublisherRegistry | None,
    ) -> None:
        self._store = store
        self._trace = trace
        self._budget = budget
        self._candidate_publishers = candidate_publishers

    def get_contract(self, contract_id: str) -> Contract | None:
        return self._store.get_contract(contract_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        return self._store.get_assets_by_name(name)

    def compute_disclosure(self, contract: Contract) -> list[Asset]:
        return compute_disclosure(contract, self._store)

    def resolve_budget(self, contract_id: str) -> int:
        return self._budget.get_remaining(contract_id)

    def can_publish_candidates(self, plugin_id: str) -> bool:
        return (
            self._candidate_publishers is not None
            and self._candidate_publishers.get(plugin_id) is not None
        )

    def publish_task_effects(
        self,
        plugin_id: str,
        effects: tuple[CandidateEffect, ...],
        *,
        idempotency_key: str,
        causal_parents: tuple[str, ...] = (),
    ) -> CommitmentDecision | None:
        publisher = (
            self._candidate_publishers.get(plugin_id)
            if self._candidate_publishers is not None
            else None
        )
        if publisher is None:
            return None
        decision = publisher.publish(
            effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )
        if decision.accepted:
            for contract in decision.contracts:
                self._budget.initialize(contract.id, contract.budget)
        return decision

    def append_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        self._trace.record(contract_id, event_type, **kwargs)

    def record_rejection(self, contract_id: str, reason: str, **kwargs: object) -> None:
        self.append_trace(
            contract_id,
            "rejection",
            rejected_fragments=[reason],
            **kwargs,
        )

    def fail_contract(
        self,
        contract: Contract,
        *,
        reason: str,
        relation_target: str = "",
    ) -> bool:
        return self._record_terminal(
            contract,
            "failed",
            reason=reason,
            relation_type="fail",
            relation_target=relation_target,
        )

    def _record_terminal(
        self,
        contract: Contract,
        event_type: str,
        *,
        reason: str,
        relation_type: str,
        relation_target: str,
    ) -> bool:
        return _commit_terminal(
            self._store,
            self._trace,
            self._budget,
            contract,
            event_type,
            relation_type=relation_type,
            relation_target=relation_target,
            reason=reason,
        )

    # These names intentionally fail closed for source-only legacy callers.
    # Production completion plugins must publish signed Candidate effects.
    def add_contract(self, contract: Contract) -> None:
        del contract
        raise RuntimeError("completion plugins must publish signed Candidate effects")

    def mint_authorized_system_asset(self, *args, **kwargs) -> Asset:
        del args, kwargs
        raise RuntimeError("completion plugins must publish signed Candidate effects")


class TaskCompletionProjector:
    """Derive consequences of one completed delegated task without local state."""

    def __init__(
        self,
        store: CompletionStoreProtocol,
        completion_registry: CompletionRegistry,
        *,
        candidate_publishers: CandidatePublisherRegistry | None = None,
    ) -> None:
        self._store = store
        self._trace = TraceManager(store)
        self._budget = BudgetManager()
        for contract in store.get_all_contracts():
            self._budget.initialize(contract.id, contract.budget)
        self._registry = completion_registry
        self._publishers = candidate_publishers
        self._context = TaskCompletionContext(
            store, self._trace, self._budget, candidate_publishers
        )

    def project(self, contract: Contract) -> bool:
        parent_id = contract.parent_id
        if contract.origin != "system" or parent_id is None:
            return False
        assets = [
            asset
            for asset in self._store.get_assets_by_contract(contract.id)
            if asset.promptable
        ]
        action_type = method_payload(contract).get("method")
        handled = False
        if isinstance(action_type, str):
            plugin = self._registry.get(action_type)
            if plugin is not None and plugin.can_handle(action_type):
                handled = plugin.handle_completion(self._context, contract, assets)

        parent = self._store.get_contract(parent_id)
        if handled:
            if parent is not None and all_outputs_satisfied(parent, self._store):
                self._complete_contract(parent)
                self._complete_satisfied_ancestors(parent)
            elif action_type == "tool" and parent is not None:
                if _tool_observation_succeeded(assets):
                    self._publish_continuation(parent, contract, assets)
                else:
                    self._record_terminal(parent, "failed")
            else:
                self._complete_satisfied_ancestors(contract)
            return True

        if parent is not None and all_outputs_satisfied(parent, self._store):
            self._complete_contract(parent)
            self._complete_satisfied_ancestors(parent)
            return True

        self._trace.record(
            parent_id,
            "task_completion_plugin_missing",
            relation_type=str(action_type),
            relation_target=contract.id,
            authority_result="rejected",
            rejected_fragments=[
                "[rejected] task_completion_plugin_missing: "
                f"no completion plugin registered for {action_type!r}"
            ],
            budget_remaining=self._budget.get_remaining(parent_id),
        )
        return False

    def _publish_continuation(
        self, parent: Contract, source: Contract, assets: list[Asset]
    ) -> None:
        from aigineering.plugins.base import PluginRequest

        plugin = ContinuationTaskPlugin()
        proposal = plugin.propose(
            PluginRequest(
                parent=parent,
                source=source,
                assets=tuple(assets),
                allowance=max(1, self._budget.get_remaining(parent.id)),
            )
        )
        continuation = contract_from_dict(proposal.effects[0].payload["contract"])
        publisher = (
            self._publishers.get(plugin.plugin_id)
            if self._publishers is not None
            else None
        )
        if publisher is None:
            self._trace.record(
                parent.id,
                "task_continuation_rejected",
                relation_type="tool",
                relation_target=continuation.id,
                authority_result="rejected",
                rejected_fragments=["[rejected] continuation publisher unavailable"],
                budget_remaining=self._budget.get_remaining(parent.id),
            )
            self._record_terminal(parent, "failed")
            return
        decision = publisher.publish(
            proposal.effects,
            idempotency_key=f"continuation:{source.id}:{continuation.id}",
            causal_parents=(source.id,),
        )
        if not decision.accepted:
            self._trace.record(
                parent.id,
                "task_continuation_rejected",
                relation_type="tool",
                relation_target=continuation.id,
                authority_result="rejected",
                rejected_fragments=["[rejected] continuation publication was rejected"],
                budget_remaining=self._budget.get_remaining(parent.id),
            )
            self._record_terminal(parent, "failed")
            return
        self._trace.record(
            parent.id,
            "task_continuation_scheduled",
            disclosed_assets=[asset.id for asset in assets],
            relation_type="tool",
            relation_target=continuation.id,
            budget_remaining=self._budget.get_remaining(parent.id),
        )

    def _complete_contract(self, contract: Contract) -> None:
        self._record_terminal(contract, "complete")

    def _complete_satisfied_ancestors(self, contract: Contract) -> None:
        parent_id = contract.parent_id
        while parent_id is not None:
            parent = self._store.get_contract(parent_id)
            if parent is None or not all_outputs_satisfied(parent, self._store):
                return
            self._complete_contract(parent)
            parent_id = parent.parent_id

    def _record_terminal(self, contract: Contract, event_type: str) -> None:
        _commit_terminal(
            self._store,
            self._trace,
            self._budget,
            contract,
            event_type,
        )


def _tool_observation_succeeded(assets: list[Asset]) -> bool:
    for asset in assets:
        try:
            payload = json.loads(asset.content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("ok") is True:
            return True
    return False


def _commit_terminal(
    store: CompletionStoreProtocol,
    trace: TraceManager,
    budget: BudgetManager,
    contract: Contract,
    event_type: str,
    *,
    relation_type: str = "",
    relation_target: str = "",
    reason: str = "",
) -> bool:
    existing_trace = any(
        entry.event_type in TERMINAL_EVENTS and entry.contract_id == contract.id
        for entry in trace.store.get_all()
    )
    existing_fact = any(
        str(record.payload.get("contract_id", "")) == contract.id
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    )
    if existing_trace or existing_fact:
        return False
    if relation_type:
        entry = create_entry(
            contract.id,
            event_type,
            relation_type=relation_type,
            relation_target=relation_target or contract.id,
            rejected_fragments=[f"[{event_type}] {relation_type}: {reason}"],
            budget_remaining=budget.get_remaining(contract.id),
        )
    else:
        entry = create_entry(
            contract.id,
            event_type,
            budget_remaining=budget.get_remaining(contract.id),
        )
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": contract.id, "terminal": event_type},
    )
    trace_record = create_runtime_record(
        "trace.recorded", {"trace": trace_entry_to_dict(entry)}
    )
    store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=[entry],
        runtime_records=(terminal, trace_record),
    )
    return True
