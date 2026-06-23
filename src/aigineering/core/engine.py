"""ACM boundary loop engine."""

from __future__ import annotations

import json
import logging

from aigineering.agent.worker import Worker
from aigineering.core.activation import check_activation
from aigineering.core.budget_manager import BudgetManager
from aigineering.core.crash import check_crash_point
from aigineering.core.context_overflow import ContextOverflowHandler
from aigineering.core.disclosure import compute_disclosure, redact_for_disclosure
from aigineering.core.labels import Label, resolve_contract_labels
from aigineering.core.ids import hash_contract
from aigineering.core.methods import (
    method_contract,
    method_context_content,
    method_payload,
    system_asset,
)
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.projection import project_candidate
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.state_serializer import StateSerializer, TraceStateRebuilder
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import MemoryTraceStore, TraceStoreProtocol
from aigineering.core.trace_manager import TraceManager
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.actions import (
    WorkerAction,
    parse_method_action,
)
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
        worker: Worker,
        trace_store: TraceStoreProtocol | None = None,
        labels: dict[str, Label] | None = None,
        tools: ToolRegistry | None = None,
        mcp_servers: dict[str, object] | None = None,
        method_registry: MethodRegistry | None = None,
        context_size_limit: int | None = None,
        ingress: RuntimeIngress | None = None,
    ) -> None:
        self._store = store
        self._worker = worker
        self._trace = trace_store if trace_store is not None else MemoryTraceStore()
        self._trace_mgr = TraceManager(self._trace)
        self._labels = labels if labels is not None else {}
        self._tools = tools
        self._mcp_servers: dict[str, object] = mcp_servers or {}
        self._method_registry = method_registry
        self._overflow_handler = ContextOverflowHandler(context_size_limit)
        self._label_context: dict[str, list[Asset]] = {}
        self._method_context: dict[str, list[Asset]] = {}
        self._budget_mgr = BudgetManager()
        self._completed: set[str] = set()
        self._suspended: set[str] = set()
        self._method_scheduled: set[str] = set()
        self._ingress = (
            ingress
            if ingress is not None
            else RuntimeIngress(store, self._trace, FactReducer(store, self._trace))
        )

    @property
    def _budget(self) -> dict[str, int]:
        """Compatibility snapshot for existing private-state tests."""
        return self._budget_mgr.get_all()

    @property
    def _contract_last_entry(self) -> dict[str, str]:
        """Compatibility snapshot for existing private-state tests."""
        return self._trace_mgr.get_all_last_entries()

    @property
    def _context_size_limit(self) -> int | None:
        """Compatibility accessor for tests that adjust the overflow limit."""
        return self._overflow_handler.limit

    @_context_size_limit.setter
    def _context_size_limit(self, value: int | None) -> None:
        self._overflow_handler = ContextOverflowHandler(value)

    def add_contract(self, contract: Contract) -> None:
        self._ingress.accept_contract(contract)
        self._budget_mgr.initialize(contract.id, contract.budget)
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
        self._ingress.accept_asset(asset, source="engine")

    def _add_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        self._trace_mgr.record(contract_id, event_type, **kwargs)

    def _commit(self, result: ProjectionResult) -> None:
        for asset in result.accepted_assets:
            self._ingress.accept_asset(asset, source="projection", allow_protected=True)

    def run(self) -> None:
        while True:
            available_names: set[str] = {a.name for a in self._store.get_all_assets()}

            enabled: list[Contract] = [
                c
                for c in self._store.get_all_contracts()
                if c.id not in self._completed
                and c.id not in self._suspended
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

                if self._check_context_overflow(contract, scope):
                    break

                if self._run_system_method(contract):
                    remaining = self._budget_mgr.consume(contract.id)
                    self._add_trace(
                        contract.id,
                        "budget_consumed",
                        relation_type=method_payload(contract).get("method"),
                        budget_remaining=remaining,
                    )
                    if self._all_outputs_satisfied(contract):
                        self._add_trace(
                            contract.id,
                            "complete",
                            budget_remaining=self._resolve_budget(contract),
                        )
                        self._completed.add(contract.id)
                        check_crash_point("after_child_complete")
                        self._resume_parent_from_method(contract)
                    break

                candidate: Candidate = self._worker.invoke(contract, scope)
                action = parse_method_action(candidate)
                if action is not None:
                    self._dispatch_method(contract, action, candidate)
                    break

                result = project_candidate(contract, candidate)
                self._commit(result)
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
                        json.dumps(dict(result.authority_policy), sort_keys=True)
                        if result.authority_policy is not None
                        else None
                    ),
                    budget_remaining=self._resolve_budget(contract),
                    usage_metadata=candidate.metadata,
                )

                remaining = self._budget_mgr.consume(contract.id)
                self._add_trace(
                    contract.id,
                    "budget_consumed",
                    budget_remaining=remaining,
                )

                if self._all_outputs_satisfied(contract):
                    self._add_trace(
                        contract.id,
                        "complete",
                        budget_remaining=self._resolve_budget(contract),
                    )
                    self._completed.add(contract.id)
                    check_crash_point("after_child_complete")
                    self._resume_parent_from_method(contract)
                    self._complete_satisfied_ancestors(contract)
                    break

    def _resolve_budget(self, contract: Contract) -> int:
        if contract.id not in self._budget_mgr.get_all():
            remaining = self._budget_mgr.initialize(contract.id, contract.budget)
            self._add_trace(
                contract.id,
                "budget_initialized",
                budget_remaining=remaining,
            )
        return self._budget_mgr.get_remaining(contract.id)

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
                scope.append(redact_for_disclosure(asset))
        for asset in self._method_context.get(contract.id, []):
            if not asset.promptable:
                continue
            if asset.id not in seen:
                seen.add(asset.id)
                scope.append(redact_for_disclosure(asset))
        return scope

    def _all_outputs_satisfied(self, contract: Contract) -> bool:
        for output_name in contract.outputs:
            if not self._store.get_assets_by_name(output_name):
                return False
        return True

    def _dispatch_method(
        self,
        contract: Contract,
        action: WorkerAction,
        candidate: Candidate,
    ) -> None:
        """Dispatch a method action through the registry or built-in scheduler.

        When a handler is registered and returns True, the handler owns the
        scheduling.  Otherwise the engine uses the default method scheduler.
        Budget decrement and parent suspension always run.
        """
        handler = None
        if self._method_registry is not None:
            handler = self._method_registry.get(action.type)

        handled = False
        if handler is not None and handler.can_handle(action.type):
            runtime = MethodRuntime(
                store=self._store,
                trace=self._trace_mgr,
                budget=self._budget_mgr,
                tools=self._tools,
                mcp_servers=self._mcp_servers,
                suspended=self._suspended,
                method_scheduled=self._method_scheduled,
                ingress=self._ingress,
            )
            handled = handler.handle_method(runtime, contract, action.type, candidate)

        if not handled:
            self._schedule_method_contract(contract, action, candidate)

        check_crash_point("after_method_schedule")
        remaining = self._budget_mgr.consume(contract.id)
        self._add_trace(
            contract.id,
            "budget_consumed",
            relation_type=action.type,
            budget_remaining=remaining,
        )
        self._suspended.add(contract.id)

    def _schedule_method_contract(
        self,
        contract: Contract,
        action: WorkerAction,
        candidate: Candidate,
    ) -> None:
        child = method_contract(contract, action)
        if child.id not in self._method_scheduled:
            self.add_contract(child)
            self._create_method_context_asset(contract, action, child)
            self._method_scheduled.add(child.id)

        self._add_trace(
            contract.id,
            "method_scheduled",
            worker_id=candidate.worker_id,
            candidate_raw=candidate.raw_output,
            relation_type=action.type,
            relation_target=child.id,
            budget_remaining=self._resolve_budget(contract),
        )

    def _create_method_context_asset(
        self,
        contract: Contract,
        action: WorkerAction,
        child: Contract,
    ) -> None:
        name = f"_method_ctx_{contract.id}"
        asset = system_asset(
            name=name,
            content=method_context_content(contract, action, child),
            created_by=contract.id,
        )
        self._ingress.accept_asset(asset, source="engine", allow_protected=True)

    def _check_context_overflow(self, contract: Contract, scope: list[Asset]) -> bool:
        overflow = self._overflow_handler.check_overflow(contract, scope)
        if overflow is None:
            return False

        # Record overflow as trace event and diagnostic asset.
        # The replan is dispatched via the normal method ingress
        # (_dispatch_method → ReplanMethodHandler), NOT via Engine
        # fabricating a worker candidate.  The worker_id prefix
        # "runtime:" marks this as a kernel-generated method trigger.
        self._add_trace(
            contract.id,
            "context_overflow",
            disclosed_assets=[a.id for a in scope],
            relation_type="replan",
            relation_target="context_size_exceeded",
            rejected_fragments=[
                f"[replan_recommended] context size {overflow.estimated_tokens} "
                f"exceeds limit {overflow.limit} — replan recommended"
            ],
            budget_remaining=self._resolve_budget(contract),
        )

        report_asset = self._overflow_handler.create_report_asset(contract.id, overflow)
        self._ingress.accept_asset(report_asset, source="engine")

        action = WorkerAction(
            type="replan",
            payload={"reason": "context_size_exceeded"},
        )
        candidate = Candidate(
            worker_id="runtime:context_overflow",
            raw_output='/replan {"reason": "context_size_exceeded"}',
        )
        self._dispatch_method(contract, action, candidate)
        return True

    def _run_system_method(self, contract: Contract) -> bool:
        method = method_payload(contract)
        if contract.origin != "system":
            return False

        method_type = method.get("method")
        if not isinstance(method_type, str):
            self._add_trace(
                contract.id,
                "method_handler_missing",
                authority_result="rejected",
                rejected_fragments=[
                    "[rejected] method_handler_missing: missing method type"
                ],
                budget_remaining=self._resolve_budget(contract),
            )
            return True

        if method_type != "tool":
            return False

        if self._method_registry is not None:
            handler = self._method_registry.get(method_type)
            if handler is not None and handler.can_handle(method_type):
                completion = getattr(handler, "handle_completion", None)
                if callable(completion) and completion(
                    _make_runtime(self), contract, []
                ):
                    return True

        self._add_trace(
            contract.id,
            "method_handler_missing",
            relation_type=method_type,
            authority_result="rejected",
            rejected_fragments=[
                "[rejected] method_handler_missing: "
                f"no handler registered for {method_type!r}"
            ],
            budget_remaining=self._resolve_budget(contract),
        )
        return True

    def _resume_parent_from_method(self, contract: Contract) -> None:
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
                    if completion(_make_runtime(self), contract, method_assets):
                        parent = self._store.get_contract(parent_id)
                        if parent is not None and self._all_outputs_satisfied(parent):
                            self._complete_contract(parent)
                            self._complete_satisfied_ancestors(parent)
                        elif method_type == "tool" and parent is not None:
                            self._schedule_continuation_contract(
                                parent, contract, method_assets
                            )
                        else:
                            self._complete_satisfied_ancestors(contract)
                        return

        parent = self._store.get_contract(parent_id)
        if parent is not None and self._all_outputs_satisfied(parent):
            self._complete_contract(parent)
            self._complete_satisfied_ancestors(parent)
            return

        if method_type == "tool" and parent is not None:
            self._schedule_continuation_contract(parent, contract, method_assets)
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

    def _complete_contract(self, contract: Contract) -> None:
        if contract.id in self._completed:
            return
        self._add_trace(
            contract.id,
            "complete",
            budget_remaining=self._resolve_budget(contract),
        )
        self._completed.add(contract.id)
        self._suspended.discard(contract.id)

    def _complete_satisfied_ancestors(self, contract: Contract) -> None:
        parent_id = contract.parent_id
        while parent_id is not None:
            parent = self._store.get_contract(parent_id)
            if parent is None or not self._all_outputs_satisfied(parent):
                return
            self._complete_contract(parent)
            parent_id = parent.parent_id

    def _schedule_continuation_contract(
        self,
        parent: Contract,
        source_contract: Contract,
        method_assets: list[Asset],
    ) -> None:
        method = method_payload(source_contract).get("method", "method")
        budget = max(1, self._budget_mgr.get_remaining(parent.id))
        name = f"{parent.name or parent.id}.{method}.continue.{source_contract.id}"
        continuation = Contract(
            id=hash_contract(
                name=name,
                description=parent.description,
                inputs=[],
                outputs=list(parent.outputs),
                activation="",
                budget=budget,
                tool_scope=list(parent.tool_scope),
                labels=list(parent.labels),
                origin="continuation",
            ),
            parent_id=parent.id,
            name=name,
            description=parent.description,
            outputs=parent.outputs,
            activation="",
            budget=budget,
            tool_scope=parent.tool_scope,
            labels=parent.labels,
            origin="continuation",
            minting_authority=parent.minting_authority,
            sensitive_input_policy=parent.sensitive_input_policy,
        )
        if continuation.id not in self._method_scheduled:
            self.add_contract(continuation)
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

    # ── State persistence / recovery ──────────────────────────────────

    def save_state(self) -> dict:
        """Serialize engine runtime state for recovery."""
        return StateSerializer.serialize(
            budget_mgr=self._budget_mgr,
            completed=self._completed,
            suspended=self._suspended,
            method_scheduled=self._method_scheduled,
            method_context=self._method_context,
            label_context=self._label_context,
            trace_mgr=self._trace_mgr,
        )

    @classmethod
    def restore(
        cls,
        store: StoreProtocol,
        worker: Worker,
        state: dict,
        trace_store: TraceStoreProtocol | None = None,
        labels: dict[str, Label] | None = None,
        tools: ToolRegistry | None = None,
        method_registry: MethodRegistry | None = None,
        context_size_limit: int | None = None,
    ) -> "Engine":
        """Restore engine from serialized state."""
        engine = cls(
            store,
            worker,
            trace_store,
            labels,
            tools,
            method_registry,
            context_size_limit=context_size_limit,
        )
        engine_state = StateSerializer.deserialize(store, state)
        engine._budget_mgr.restore(engine_state.budget)
        engine._completed = engine_state.completed
        engine._suspended = engine_state.suspended
        engine._method_scheduled = engine_state.method_scheduled
        engine._method_context = engine_state.method_context
        engine._label_context = engine_state.label_context
        engine._trace_mgr.restore_last_entries(engine_state.contract_last_entry)
        return engine

    @classmethod
    def restore_from_store(
        cls,
        store: StoreProtocol,
        worker: Worker,
        trace_store: TraceStoreProtocol,
        labels: dict[str, Label] | None = None,
        tools: ToolRegistry | None = None,
        method_registry: MethodRegistry | None = None,
        context_size_limit: int | None = None,
    ) -> "Engine":
        """Reconstruct engine state from store records and trace events."""
        engine = cls(
            store,
            worker,
            trace_store,
            labels,
            tools,
            method_registry,
            context_size_limit=context_size_limit,
        )

        engine_state = TraceStateRebuilder.rebuild(store, trace_store)
        engine._budget_mgr.restore(engine_state.budget)
        engine._completed = engine_state.completed
        engine._suspended = engine_state.suspended
        engine._method_scheduled = engine_state.method_scheduled
        engine._method_context = engine_state.method_context
        engine._label_context = engine_state.label_context
        engine._trace_mgr.restore_last_entries(engine_state.contract_last_entry)

        return engine


def _make_runtime(engine: Engine) -> MethodRuntime:
    """Create a MethodRuntime from an Engine instance."""
    return MethodRuntime(
        store=engine._store,
        trace=engine._trace_mgr,
        budget=engine._budget_mgr,
        tools=engine._tools,
        mcp_servers=engine._mcp_servers,
        suspended=engine._suspended,
        method_scheduled=engine._method_scheduled,
    )
