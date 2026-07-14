"""Continuation manager — handles method completion and continuation contract scheduling.

Extracted from Engine to own the parent-resume and continuation-scheduling
logic.  Receives all dependencies through explicit injection so that Engine
delegates cleanly without exposing private internals.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from aigineering.core.ids import hash_contract_v2
from aigineering.core.labels import resolve_contract_labels
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.methods import method_payload
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.protocol.types import Contract

if TYPE_CHECKING:
    from aigineering.core.budget_manager import BudgetManager
    from aigineering.core.labels import Label
    from aigineering.core.method_registry import MethodRegistry
    from aigineering.core.runtime_ingress import RuntimeIngress
    from aigineering.core.store import StoreProtocol
    from aigineering.core.tools import ToolRegistry
    from aigineering.core.trace_manager import TraceManager
    from aigineering.protocol.types import Asset, TraceEntry


class ContinuationManager:
    """Resumes parents after method contracts complete; schedules continuation contracts.

    Accepts all mutable engine state (completed, suspended, method_scheduled,
    method_context, pending_trace_entries) via explicit dependency injection so
    that :class:`Engine` can delegate without exposing private members.
    """

    _TERMINAL_EVENTS = frozenset({"complete", "failed", "cancelled", "unreachable"})

    def __init__(
        self,
        store: StoreProtocol,
        budget_mgr: BudgetManager,
        trace_mgr: TraceManager,
        method_registry: MethodRegistry | None,
        completed: set[str],
        suspended: set[str],
        method_scheduled: set[str],
        method_context: dict[str, list[Asset]],
        tools: ToolRegistry | None = None,
        mcp_servers: dict[str, object] | None = None,
        ingress: RuntimeIngress | None = None,
        pending_trace_entries: list[TraceEntry] | None = None,
        labels: dict[str, Label] | None = None,
        label_mode: str = "debug",
        label_context: dict[str, list[Asset]] | None = None,
    ) -> None:
        self._store = store
        self._budget_mgr = budget_mgr
        self._trace_mgr = trace_mgr
        self._method_registry = method_registry
        self._completed = completed
        self._suspended = suspended
        self._method_scheduled = method_scheduled
        self._method_context = method_context
        self._tools = tools
        self._mcp_servers: dict[str, object] = mcp_servers or {}
        self._ingress = ingress
        self._pending_trace_entries: list[TraceEntry] = (
            pending_trace_entries if pending_trace_entries is not None else []
        )
        self._labels: dict[str, Label] = labels if labels is not None else {}
        self._label_mode = label_mode
        self._label_context: dict[str, list[Asset]] = (
            label_context if label_context is not None else {}
        )

    # ── Public API ──────────────────────────────────────────────────────

    def resume_parent_from_method(self, contract: Contract) -> None:
        """Resume the parent contract after a system method contract completes.

        Called by :class:`Engine` when a method contract (plan, tool, replan,
        etc.) reaches completion.  The method handler may expand results,
        trigger continuation scheduling, or complete the parent inline.
        """
        parent_id = contract.parent_id
        if contract.origin != "system" or parent_id is None:
            return

        method_assets = [
            asset
            for asset in self._store.get_assets_by_contract(contract.id)
            if asset.promptable
        ]
        method_type = method_payload(contract).get("method")
        if self._method_registry is not None and isinstance(method_type, str):
            handler = self._method_registry.get(method_type)
            if handler is not None and handler.can_handle(method_type):
                completion = getattr(handler, "handle_completion", None)
                if callable(completion):
                    runtime = MethodRuntime(
                        store=self._store,
                        trace=self._trace_mgr,
                        budget=self._budget_mgr,
                        tools=self._tools,
                        mcp_servers=self._mcp_servers,
                        suspended=self._suspended,
                        method_scheduled=self._method_scheduled,
                    )
                    if completion(runtime, contract, method_assets):
                        if method_type == "tool":
                            method_assets = [
                                asset
                                for output in contract.outputs
                                if output.startswith(("_tool_obs_", "_mcp_obs_"))
                                for asset in self._store.get_assets_by_name(output)
                                if asset.promptable
                            ]
                        parent = self._store.get_contract(parent_id)
                        if parent is not None and self._all_outputs_satisfied(parent):
                            self.complete_contract(parent)
                            self.complete_satisfied_ancestors(parent)
                        elif method_type == "tool" and parent is not None:
                            if _tool_observation_succeeded(method_assets):
                                self.schedule_continuation_contract(
                                    parent, contract, method_assets
                                )
                            else:
                                self._emit_terminal_event(
                                    parent.id,
                                    "failed",
                                    budget_remaining=self._resolve_budget(parent),
                                )
                                self._completed.add(parent.id)
                                self._suspended.discard(parent.id)
                        else:
                            self.complete_satisfied_ancestors(contract)
                        return

        parent = self._store.get_contract(parent_id)
        if parent is not None and self._all_outputs_satisfied(parent):
            self.complete_contract(parent)
            self.complete_satisfied_ancestors(parent)
            return

        if method_type == "tool" and parent is not None:
            if _tool_observation_succeeded(method_assets):
                self.schedule_continuation_contract(parent, contract, method_assets)
            else:
                self._emit_terminal_event(
                    parent.id,
                    "failed",
                    budget_remaining=self._resolve_budget(parent),
                )
                self._completed.add(parent.id)
                self._suspended.discard(parent.id)
            return

        self._add_trace(
            parent_id,
            "method_handler_missing",
            relation_type=str(method_type),
            relation_target=contract.id,
            authority_result="rejected",
            rejected_fragments=[
                "[rejected] method_handler_missing: "
                f"no completion handler registered for {method_type!r}"
            ],
            budget_remaining=self._budget_mgr.get_remaining(parent_id),
        )

    def schedule_continuation_contract(
        self,
        parent: Contract,
        source_contract: Contract,
        method_assets: list[Asset],
    ) -> None:
        """Schedule a continuation contract from tool observation assets.

        Creates a new contract derived from *parent* that continues the work
        with the tool observation context.
        """
        method = method_payload(source_contract).get("method", "method")
        budget = max(1, self._budget_mgr.get_remaining(parent.id))
        name = f"{parent.name or parent.id}.{method}.continue.{source_contract.id}"
        continuation = Contract(
            id=hash_contract_v2(
                name=name,
                description=parent.description,
                inputs=[],
                outputs=list(parent.outputs),
                activation="",
                budget=budget,
                tool_scope=list(parent.tool_scope),
                labels=list(parent.labels),
                worker_capabilities=list(parent.worker_capabilities),
                worker_pools=list(parent.worker_pools),
                origin="continuation",
                parent_id=parent.id,
            ),
            parent_id=parent.id,
            name=name,
            description=parent.description,
            outputs=parent.outputs,
            activation="",
            budget=budget,
            tool_scope=parent.tool_scope,
            labels=parent.labels,
            worker_capabilities=parent.worker_capabilities,
            worker_pools=parent.worker_pools,
            origin="continuation",
            minting_authority=parent.minting_authority,
            sensitive_input_policy=parent.sensitive_input_policy,
        )
        if continuation.id not in self._method_scheduled:
            self._add_contract(continuation)
            self._method_scheduled.add(continuation.id)
            if method_assets:
                self._method_context[continuation.id] = list(method_assets)

        self._add_trace(
            parent.id,
            "method_continuation_scheduled",
            disclosed_assets=[asset.id for asset in method_assets],
            relation_type=str(method),
            relation_target=continuation.id,
            budget_remaining=self._budget_mgr.get_remaining(parent.id),
        )

    # ── Internal helpers ─────────────────────────────────────────────────

    def complete_contract(self, contract: Contract) -> None:
        if contract.id in self._completed:
            return
        self._emit_terminal_event(
            contract.id,
            "complete",
            budget_remaining=self._resolve_budget(contract),
        )
        self._completed.add(contract.id)
        self._suspended.discard(contract.id)

    def complete_satisfied_ancestors(self, contract: Contract) -> None:
        parent_id = contract.parent_id
        while parent_id is not None:
            parent = self._store.get_contract(parent_id)
            if parent is None or not self._all_outputs_satisfied(parent):
                return
            self.complete_contract(parent)
            parent_id = parent.parent_id

    def _all_outputs_satisfied(self, contract: Contract) -> bool:
        return all_outputs_satisfied(contract, self._store)

    def _resolve_budget(self, contract: Contract) -> int:
        if contract.id not in self._budget_mgr.get_all():
            remaining = self._budget_mgr.initialize(contract.id, contract.budget)
            self._add_trace(
                contract.id,
                "budget_initialized",
                budget_remaining=remaining,
            )
        return self._budget_mgr.get_remaining(contract.id)

    def _emit_terminal_event(
        self, contract_id: str, event_type: str, **kwargs: object
    ) -> None:
        existing = [
            e
            for e in self._trace_mgr.store.get_all()
            if e.event_type == event_type and e.contract_id == contract_id
        ]
        if existing:
            return
        self._add_trace(contract_id, event_type, **kwargs)

    def _add_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        self._trace_mgr.record(contract_id, event_type, **kwargs)

    def _add_contract(self, contract: Contract) -> None:
        if self._ingress is not None:
            self._ingress.accept_contract(contract)
        self._budget_mgr.initialize(contract.id, contract.budget)
        if contract.labels:
            resolution = resolve_contract_labels(
                contract,
                self._labels,
                self._store,
                ingress=self._ingress,
                mode=self._label_mode,
            )
            self._label_context[contract.id] = resolution.injected_assets
            self._add_trace(
                contract.id,
                "label_resolved",
                disclosed_assets=[asset.id for asset in resolution.injected_assets],
                relation_type="label",
                relation_target=",".join(resolution.label_names),
                budget_remaining=self._resolve_budget(contract),
            )


def _tool_observation_succeeded(method_assets: list["Asset"]) -> bool:
    """Return true only for an explicit successful tool observation."""
    for asset in method_assets:
        if asset.name.startswith(("_tool_obs_", "_mcp_obs_")):
            try:
                return json.loads(asset.content).get("ok") is True
            except json.JSONDecodeError:
                return False
    return False
