"""ACM boundary loop engine."""

from __future__ import annotations

import json
import logging
import time as _time_module
from typing import Callable

from aigineering.agent.worker import Worker
from aigineering.core.budget_manager import BudgetManager
from aigineering.core.crash import check_crash_point
from aigineering.core.context_overflow import (
    ContextOverflowHandler,
    ContextOverflowOrchestrator,
)
from aigineering.core.continuation_manager import ContinuationManager
from aigineering.core.disclosure import compute_disclosure, redact_for_disclosure
from aigineering.core.labels import Label, LABEL_MODE_DEBUG, resolve_contract_labels
from aigineering.core.method_handlers.recovery import (
    schedule_method_result_recovery,
    schedule_projection_recovery,
)
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.methods import method_payload
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.projection import project_candidate
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.state_serializer import (
    StateSerializer,
    TraceStateRebuilder,
)
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import MemoryTraceStore, TraceStoreProtocol
from aigineering.core.trace_manager import TraceManager
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.actions import (
    WorkerAction,
    parse_method_action,
)
from aigineering.protocol.types import (
    Asset,
    Candidate,
    Contract,
    ProjectionResult,
    TraceEntry,
)

_logger = logging.getLogger(__name__)


class _TraceManagerProxy(TraceManager):
    def __init__(self, delegate: TraceManager) -> None:
        self._delegate = delegate

    @property
    def store(self) -> TraceStoreProtocol:
        return self._delegate.store

    def record(self, contract_id: str, event_type: str, **kwargs: object) -> TraceEntry:
        return self._delegate.record(contract_id, event_type, **kwargs)

    def get_last_entry_id(self, contract_id: str) -> str | None:
        return self._delegate.get_last_entry_id(contract_id)

    def get_all_last_entries(self) -> dict[str, str]:
        return self._delegate.get_all_last_entries()

    def restore_last_entries(self, entries: dict[str, str]) -> None:
        self._delegate.restore_last_entries(entries)


class _CapturingMemoryTraceStore:
    def __init__(self, pending: list[TraceEntry]) -> None:
        self._inner = MemoryTraceStore()
        self._pending = pending

    @property
    def entries(self) -> list[TraceEntry]:
        return self._inner.entries

    def append(self, entry: TraceEntry) -> None:
        self._inner.append(entry)
        self._pending.append(entry)

    def get_all(self) -> list[TraceEntry]:
        return self._inner.get_all()

    def get_by_contract(self, contract_id: str) -> list[TraceEntry]:
        return self._inner.get_by_contract(contract_id)

    def get_by_event_type(self, event_type: str) -> list[TraceEntry]:
        return self._inner.get_by_event_type(event_type)

    def get_reverse_lineage(self, asset_id: str) -> list[TraceEntry]:
        return self._inner.get_reverse_lineage(asset_id)

    def new_entry(
        self, contract_id: str, event_type: str, **kwargs: object
    ) -> TraceEntry:
        entry = self._inner.new_entry(contract_id, event_type, **kwargs)
        self._pending.append(entry)
        return entry


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
        label_mode: str = LABEL_MODE_DEBUG,
    ) -> None:
        self._store = store
        self._worker = worker
        self._pending_trace_entries: list[TraceEntry] = []
        self._pending_assets: list[Asset] = []
        self._persist_trace: TraceStoreProtocol | None = trace_store
        self._trace = _CapturingMemoryTraceStore(self._pending_trace_entries)
        self._trace_mgr = _TraceManagerProxy(TraceManager(self._trace))
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
        self._label_mode = label_mode
        self._ingress = (
            ingress
            if ingress is not None
            else RuntimeIngress(store, self._trace, FactReducer(store, self._trace))
        )
        self._continuations = ContinuationManager(
            store=self._store,
            budget_mgr=self._budget_mgr,
            trace_mgr=self._trace_mgr,
            completion_registry=self._method_registry,
            completed=self._completed,
            suspended=self._suspended,
            method_scheduled=self._method_scheduled,
            method_context=self._method_context,
            tools=self._tools,
            mcp_servers=self._mcp_servers,
            ingress=self._ingress,
            pending_trace_entries=self._pending_trace_entries,
            labels=self._labels,
            label_mode=self._label_mode,
            label_context=self._label_context,
        )
        self._overflow = ContextOverflowOrchestrator(
            self._overflow_handler, self._ingress
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
        self._overflow = ContextOverflowOrchestrator(
            self._overflow_handler, self._ingress
        )

    def add_contract(self, contract: Contract) -> None:
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
        self._write_pending_traces()

    def add_asset(self, asset: Asset) -> None:
        self._ingress.accept_asset(asset, source="engine")
        self._write_pending_traces()

    def inject_north_star(self, goal: str) -> Asset:
        """Inject a North Star goal as a protected system asset.

        The ``_north_star_`` asset carries the top-level directive for the
        runtime session.  It is minted with ``origin="system"``,
        ``trust_tier="system"``, and is accepted with
        ``allow_protected=True`` since ``_north_star_`` is a reserved prefix.

        Returns the signed, persisted asset.
        """
        north_star = Asset(
            id=hash_asset_content("_north_star_", goal),
            name="_north_star_",
            content=goal,
            content_type="text/plain",
            definition_hash=hash_asset_definition("_north_star_"),
            content_hash=hash_asset_content("_north_star_", goal),
            origin="system",
            trust_tier="system",
            minted_by="engine",
            promptable=True,
        )
        return self._ingress.accept_asset(
            north_star, source="engine", allow_protected=True
        )

    def _add_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        self._trace_mgr.record(contract_id, event_type, **kwargs)

    def _write_pending_traces(self) -> None:
        if not self._pending_trace_entries or self._persist_trace is None:
            return
        for entry in self._pending_trace_entries:
            self._persist_trace.append(entry)
        self._pending_trace_entries.clear()

    # ── Terminal event idempotency ────────────────────────────────────

    _TERMINAL_EVENTS = frozenset({"complete", "failed", "cancelled", "unreachable"})

    def _emit_terminal_event(
        self, contract_id: str, event_type: str, **kwargs: object
    ) -> None:
        """Emit a terminal trace event with idempotency guard.

        Terminal events (complete, failed, cancelled, unreachable) are
        **immutable** — they must only be appended once per contract.
        This guard checks the trace store before appending so that
        replay and recovery always see exactly one terminal event.
        """
        existing = [
            e
            for e in self._trace_mgr.store.get_all()
            if e.event_type == event_type and e.contract_id == contract_id
        ]
        if existing:
            return
        self._add_trace(contract_id, event_type, **kwargs)

    def _sync_completed_from_trace(self) -> None:
        """Populate ``_completed`` from trace events emitted by the
        RuntimeIngress/FactReducer when assets were injected externally.

        This ensures that contracts completed via reactive projection
        (not via the Engine's own run loop) are recognised as complete.
        """
        projection = RuntimeProjection(self._store, self._trace_mgr.store)
        for contract in self._store.get_all_contracts():
            if projection.contract_view(contract).terminal is not None:
                self._completed.add(contract.id)

    def _commit(self, result: ProjectionResult) -> None:
        for asset in result.accepted_assets:
            self._pending_assets.append(asset)

    def _flush_pending(self) -> None:
        if not self._pending_assets and not self._pending_trace_entries:
            return
        trace_snapshot = list(self._pending_trace_entries)
        self._ingress.commit_execution_batch(
            assets=list(self._pending_assets),
            engine_trace_entries=trace_snapshot,
            source="projection",
            allow_protected=True,
        )
        # Mirror to external trace store so callers reading trace_store
        # (e.g. tests) see the committed entries.
        if self._persist_trace is not None:
            for entry in trace_snapshot:
                self._persist_trace.append(entry)
        self._pending_assets.clear()
        self._pending_trace_entries.clear()

    def run(self, heartbeat_callback: Callable[[], None] | None = None) -> None:
        # Sync completed contracts from trace events — the RuntimeIngress
        # may have marked contracts complete via reactive FactReducer
        # projection (e.g., external asset injection).
        self._sync_completed_from_trace()

        _heartbeat_contract_interval = 5
        _heartbeat_time_interval_s = 30.0
        contract_count = 0
        last_heartbeat = _time_module.monotonic()

        while True:
            projection = RuntimeProjection(self._store, self._trace_mgr.store)
            enabled: list[Contract] = []
            for contract in self._store.get_all_contracts():
                view = projection.contract_view(contract)
                # Output-slot satisfaction is not yet a universal execution
                # blocker: recovery contracts intentionally publish a repaired
                # version into a slot that already contains a rejected result.
                # Keep that fact visible in ContractView, but cut Engine over
                # only to the projection semantics that are already universal.
                if (
                    view.terminal is None
                    and view.activation_satisfied
                    and view.budget_remaining > 0
                    and contract.id not in self._suspended
                ):
                    enabled.append(contract)

            if not enabled:
                break

            for contract in enabled:
                # ── heartbeat: best-effort periodic renewal ──────────
                if heartbeat_callback is not None:
                    contract_count += 1
                    if (
                        contract_count % _heartbeat_contract_interval == 0
                        or _time_module.monotonic() - last_heartbeat
                        >= _heartbeat_time_interval_s
                    ):
                        try:
                            heartbeat_callback()
                        except Exception:
                            pass
                        last_heartbeat = _time_module.monotonic()
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
                        self._emit_terminal_event(
                            contract.id,
                            "complete",
                            budget_remaining=self._resolve_budget(contract),
                        )
                        self._completed.add(contract.id)
                        self._write_pending_traces()
                        check_crash_point("after_child_complete")
                        self._resume_parent_from_method(contract)
                    break

                candidate: Candidate = self._worker.invoke(contract, scope)
                action = parse_method_action(candidate)
                if action is not None:
                    if self._recover_method_contract_action(
                        contract, candidate, action
                    ):
                        break
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

                pending_names = {a.name for a in self._pending_assets}
                if self._all_outputs_satisfied(
                    contract, extra_output_names=pending_names
                ):
                    self._emit_terminal_event(
                        contract.id,
                        "complete",
                        budget_remaining=self._resolve_budget(contract),
                    )
                    self._completed.add(contract.id)
                    self._flush_pending()
                    check_crash_point("after_child_complete")
                    self._resume_parent_from_method(contract)
                    self._complete_satisfied_ancestors(contract)
                    break
                self._flush_pending()
                if self._recover_rejected_projection(
                    contract, candidate, result, rejected_dicts, remaining
                ):
                    break

        self._write_pending_traces()

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

    def _all_outputs_satisfied(
        self, contract: Contract, *, extra_output_names: set[str] | None = None
    ) -> bool:
        return all_outputs_satisfied(
            contract, self._store, extra_output_names=extra_output_names
        )

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
        runtime = _make_runtime(self)
        if self._method_registry is not None:
            handler = self._method_registry.get(action.type)

        handled = False
        if handler is not None and handler.can_handle(action.type):
            handled = handler.handle_method(runtime, contract, action.type, candidate)

        if not handled:
            runtime.schedule_method(contract, action, candidate)

        self._write_pending_traces()
        check_crash_point("after_method_schedule")
        remaining = self._budget_mgr.consume(contract.id)
        self._add_trace(
            contract.id,
            "budget_consumed",
            relation_type=action.type,
            budget_remaining=remaining,
        )
        self._suspended.add(contract.id)

    def _recover_method_contract_action(
        self,
        contract: Contract,
        candidate: Candidate,
        action: WorkerAction,
    ) -> bool:
        method_type = method_payload(contract).get("method")
        if (
            contract.origin != "system"
            or contract.parent_id is None
            or method_type not in {"plan", "replan"}
        ):
            return False
        handler = (
            self._method_registry.get(str(method_type))
            if self._method_registry is not None
            else None
        )
        if handler is None or not handler.can_handle(str(method_type)):
            return False

        runtime = _make_runtime(self)
        schedule_method_result_recovery(
            runtime,
            method_type=str(method_type),
            parent_id=contract.parent_id,
            failed_contract=contract,
            result_asset=Asset(
                id=f"candidate:{contract.id}",
                name=f"/{action.type}",
                content=candidate.raw_output,
                created_by=contract.id,
                origin="candidate",
            ),
            rejections=[
                {
                    "child_name": "(method_result)",
                    "field": "action",
                    "reason": (
                        f"method contract must produce its declared "
                        f"_{method_type}_result asset, not /{action.type}"
                    ),
                    "action": "rejected",
                    "expected": f"/exec with declared output {contract.outputs!r}",
                    "actual": candidate.raw_output[:200],
                    "recoverable": True,
                }
            ],
        )
        self._add_trace(
            contract.id,
            "projection",
            disclosed_assets=[],
            worker_id=candidate.worker_id,
            candidate_raw=candidate.raw_output,
            rejected_fragments=[
                f"[parse_error] /{action.type}: method contract must produce "
                f"declared output {contract.outputs!r}"
            ],
            authority_result="rejected",
            budget_remaining=self._resolve_budget(contract),
            usage_metadata=candidate.metadata,
        )
        remaining = self._budget_mgr.consume(contract.id)
        self._add_trace(
            contract.id,
            "budget_consumed",
            relation_type=str(method_type),
            budget_remaining=remaining,
        )
        self._emit_terminal_event(
            contract.id,
            "failed",
            relation_type=str(method_type),
            relation_target=action.type,
            budget_remaining=remaining,
        )
        self._completed.add(contract.id)
        self._suspended.discard(contract.id)
        return True

    def _recover_rejected_projection(
        self,
        contract: Contract,
        candidate: Candidate,
        result: ProjectionResult,
        rejected_dicts: list[dict],
        budget_remaining: int,
    ) -> bool:
        if result.status.value != "rejected" or not rejected_dicts:
            return False

        method_type = method_payload(contract).get("method")
        runtime = _make_runtime(self)
        if (
            contract.origin == "system"
            and contract.parent_id is not None
            and method_type in {"plan", "replan"}
        ):
            handler = (
                self._method_registry.get(str(method_type))
                if self._method_registry is not None
                else None
            )
            if handler is None or not handler.can_handle(str(method_type)):
                return False
            schedule_method_result_recovery(
                runtime,
                method_type=str(method_type),
                parent_id=contract.parent_id,
                failed_contract=contract,
                result_asset=Asset(
                    id=f"candidate:{contract.id}",
                    name=f"_{method_type}_result_rejected",
                    content=candidate.raw_output,
                    created_by=contract.id,
                    origin="candidate",
                ),
                rejections=[
                    {
                        "child_name": "(method_result)",
                        "field": entry["category"],
                        "reason": entry["reject_reason"],
                        "action": "rejected",
                        "expected": f"/exec with declared output {contract.outputs!r}",
                        "actual": entry["content"][:200],
                        "recoverable": True,
                    }
                    for entry in rejected_dicts
                ],
            )
        else:
            schedule_projection_recovery(
                runtime,
                failed_contract=contract,
                candidate_raw=candidate.raw_output,
                rejections=rejected_dicts,
            )

        self._emit_terminal_event(
            contract.id,
            "failed",
            relation_type="projection",
            relation_target="recovery",
            budget_remaining=budget_remaining,
        )
        self._completed.add(contract.id)
        self._suspended.discard(contract.id)
        return True

    def _check_context_overflow(self, contract: Contract, scope: list[Asset]) -> bool:
        return self._overflow.handle_overflow(
            contract,
            scope,
            budget_remaining=self._resolve_budget(contract),
            add_trace=self._add_trace,
            dispatch_method=self._dispatch_method,
        )

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
        self._continuations.resume_parent_from_method(contract)

    def _complete_contract(self, contract: Contract) -> None:
        self._continuations.complete_contract(contract)

    def _complete_satisfied_ancestors(self, contract: Contract) -> None:
        self._continuations.complete_satisfied_ancestors(contract)

    def _schedule_continuation_contract(
        self,
        parent: Contract,
        source_contract: Contract,
        method_assets: list[Asset],
    ) -> None:
        self._continuations.schedule_continuation_contract(
            parent, source_contract, method_assets
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
            store=store,
            worker=worker,
            trace_store=trace_store,
            labels=labels,
            tools=tools,
            method_registry=method_registry,
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
        """Reconstruct engine state from store records and trace events.

        Completion is derived from **durable store facts** (output
        satisfaction), not from trace ``complete`` events.  Trace-based
        reconstruction runs as an overlay for method-level state
        (suspended, method_scheduled, method/label context, and budget
        consumption).
        """
        engine = cls(
            store=store,
            worker=worker,
            trace_store=trace_store,
            labels=labels,
            tools=tools,
            method_registry=method_registry,
            context_size_limit=context_size_limit,
        )

        # Load persisted trace events into the engine's runtime trace store
        # (always MemoryTraceStore) so that _emit_terminal_event idempotency
        # checks and _sync_completed_from_trace work correctly after restore.
        for entry in trace_store.get_all():
            engine._trace.append(entry)
        # Clear pending — loaded historical trace must not be re-committed.
        engine._pending_trace_entries.clear()

        # Trace events reconstruct method-level compatibility caches. Contract
        # completion/readiness itself comes from the same pure projection used
        # by the live scheduling loop.
        trace_state = TraceStateRebuilder.rebuild(store, trace_store)
        projection = RuntimeProjection(store, trace_store)
        projected_completed: set[str] = set()
        for contract in store.get_all_contracts():
            view = projection.contract_view(contract)
            if view.terminal is not None or view.outputs_satisfied:
                projected_completed.add(contract.id)
            if view.terminal == "conflict":
                _logger.error(
                    "restore_from_store: conflicting terminal facts for contract %s: %s",
                    contract.id,
                    view.terminal_events,
                )

        engine._budget_mgr.restore(trace_state.budget)
        engine._completed = projected_completed
        engine._suspended = trace_state.suspended
        engine._method_scheduled = trace_state.method_scheduled
        engine._method_context = trace_state.method_context
        engine._label_context = trace_state.label_context
        engine._trace_mgr.restore_last_entries(trace_state.contract_last_entry)

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
        ingress=engine._ingress,
    )


def derive_task_tree(contracts: list[Contract]) -> dict[str, list[str]]:
    """Derive task tree outline from parent_id relationships.

    Returns ``{parent_id: [child_ids]}`` mapping.  Root tasks have
    ``parent_id=None`` and map to the ``"__root__"`` key.

    This is a **projection**, not a second graph source.  The canonical
    relationship lives in ``Contract.parent_id``.
    """
    tree: dict[str, list[str]] = {}
    for c in contracts:
        parent = c.parent_id or "__root__"
        tree.setdefault(parent, []).append(c.id)
    return tree
