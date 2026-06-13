"""ACM boundary loop engine."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from aigineering.agent.worker import Worker
from aigineering.core.activation import check_activation
from aigineering.core.disclosure import compute_disclosure, redact_for_disclosure
from aigineering.core.labels import Label, resolve_contract_labels
from aigineering.core.methods import (
    contracts_from_plan_asset,
    method_contract,
    method_payload,
    system_asset,
)
from aigineering.core.projection import project_candidate
from aigineering.core.provenance import sign_asset
from aigineering.core.method_registry import MethodRegistry
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import MemoryTraceStore, TraceStoreProtocol
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.actions import (
    ActionParseError,
    WorkerAction,
    action_from_dict,
    parse_action,
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
    # Rough token estimate: ~4 chars per token for English text.
    _CHARS_PER_TOKEN = 4

    def __init__(
        self,
        store: StoreProtocol,
        worker: Worker,
        trace_store: TraceStoreProtocol | None = None,
        labels: dict[str, Label] | None = None,
        tools: ToolRegistry | None = None,
        method_registry: MethodRegistry | None = None,
        context_size_limit: int | None = None,
    ) -> None:
        self._store = store
        self._worker = worker
        self._trace = trace_store if trace_store is not None else MemoryTraceStore()
        self._labels = labels if labels is not None else {}
        self._tools = tools
        self._method_registry = method_registry
        self._context_size_limit = context_size_limit  # None = no limit
        self._label_context: dict[str, list[Asset]] = {}
        self._method_context: dict[str, list[Asset]] = {}
        self._budget: dict[str, int] = {}
        self._completed: set[str] = set()
        self._suspended: set[str] = set()
        self._method_scheduled: set[str] = set()
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
        signed = sign_asset(asset)
        if not signed.signed_by:
            signed = sign_asset(asset, signed_by="engine")
        self._store.add_asset(signed)

    def _add_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        parent_id = self._contract_last_entry.get(contract_id)
        entry = self._trace.new_entry(contract_id, event_type, parent_id=parent_id, **kwargs)
        self._contract_last_entry[contract_id] = entry.id

    def _commit(self, result: ProjectionResult) -> None:
        for asset in result.accepted_assets:
            signed = sign_asset(asset)
            if not signed.signed_by:
                signed = sign_asset(asset, signed_by="engine")
            self._store.add_asset(signed)

    def run(self) -> None:
        while True:
            available_names: set[str] = {
                a.name for a in self._store.get_all_assets()
            }

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
                    remaining = self._resolve_budget(contract)
                    self._budget[contract.id] = max(0, remaining - 1)
                    if self._all_outputs_satisfied(contract):
                        self._add_trace(
                            contract.id,
                            "complete",
                            budget_remaining=self._resolve_budget(contract),
                        )
                        self._completed.add(contract.id)
                        self._resume_parent_from_method(contract)
                    break

                candidate: Candidate = self._worker.invoke(contract, scope)
                action = parse_method_action(candidate)
                if action is not None:
                    self._dispatch_method(contract, action, candidate)
                    break

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
                        json.dumps(dict(result.authority_policy), sort_keys=True)
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
                    self._resume_parent_from_method(contract)
                    break

    def _resolve_budget(self, contract: Contract) -> int:
        if contract.id not in self._budget:
            self._budget[contract.id] = max(contract.budget, 1)
            self._add_trace(
                contract.id,
                "budget_initialized",
                budget_remaining=self._budget[contract.id],
            )
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
            matching = self._store.get_assets_by_name(output_name)
            if not any(a.created_by == contract.id for a in matching):
                return False
        return True

    def _dispatch_method(
        self,
        contract: Contract,
        action: WorkerAction,
        candidate: Candidate,
    ) -> None:
        """Dispatch a method action through the registry or inline fallback.

        When a handler is registered and returns True, the handler owns the
        scheduling.  Otherwise the engine uses the default inline scheduling.
        Budget decrement and parent suspension always run.
        """
        handler = None
        if self._method_registry is not None:
            handler = self._method_registry.get(action.type)

        handled = False
        if handler is not None and handler.can_handle(action.type):
            runtime = MethodRuntime(
                store=self._store,
                trace=self._trace,
                budget=self._budget,
                tools=self._tools,
                suspended=self._suspended,
                method_scheduled=self._method_scheduled,
            )
            handled = handler.handle_method(runtime, contract, action.type, candidate)

        if not handled:
            self._schedule_method_contract(contract, action, candidate)

        remaining = self._resolve_budget(contract)
        consumed = 1
        self._budget[contract.id] = max(0, remaining - consumed)
        self._add_trace(
            contract.id,
            "budget_consumed",
            relation_type=action.type,
            budget_remaining=self._budget[contract.id],
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
        content = json.dumps(
            {
                "method": action.type,
                "parent_contract_id": contract.id,
                "child_contract_id": child.id,
                "payload": action.payload,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        asset = system_asset(
            name=name,
            content=content,
            created_by=contract.id,
        )
        self._store.add_asset(sign_asset(asset))

    def _estimate_context_tokens(self, scope: list[Asset]) -> int:
        total_chars = sum(len(a.content) for a in scope)
        return total_chars // self._CHARS_PER_TOKEN

    def _check_context_overflow(
        self, contract: Contract, scope: list[Asset]
    ) -> bool:
        # System method contracts are internally managed — never overflow.
        if contract.origin == "system":
            return False

        if self._context_size_limit is None:
            return False

        estimated_tokens = self._estimate_context_tokens(scope)
        if estimated_tokens <= self._context_size_limit:
            return False

        self._add_trace(
            contract.id,
            "context_overflow",
            disclosed_assets=[a.id for a in scope],
            relation_type="replan",
            relation_target="context_size_exceeded",
            rejected_fragments=[
                f"[replan_recommended] context size {estimated_tokens} "
                f"exceeds limit {self._context_size_limit} — replan recommended"
            ],
            budget_remaining=self._resolve_budget(contract),
        )

        from aigineering.protocol.types import Candidate
        from aigineering.protocol.actions import WorkerAction

        action = WorkerAction(
            type="replan",
            payload={"reason": f"context overflow ({estimated_tokens} tokens)"},
        )
        candidate = Candidate(
            worker_id="engine",
            raw_output=f'/replan {{"reason": "context overflow ({estimated_tokens} tokens)"}}',
        )
        self._dispatch_method(contract, action, candidate)
        return True

    def _run_system_method(self, contract: Contract) -> bool:
        method = method_payload(contract)
        if contract.origin != "system" or method.get("method") != "tool":
            return False

        # Tool handler takes priority when registered.
        if self._method_registry is not None:
            handler = self._method_registry.get("tool")
            if handler is not None and handler.can_handle("tool"):
                completion = getattr(handler, "handle_completion", None)
                if callable(completion) and completion(_make_runtime(self), contract, []):
                    return True

        # Fallback: inline tool execution (backward compat).
        payload = method.get("payload", {})
        tool_name = payload.get("name") if isinstance(payload, dict) else None
        args = payload.get("args", {}) if isinstance(payload, dict) else {}
        call_content = json.dumps(
            {
                "tool": tool_name,
                "args": args,
                "contract_id": contract.id,
                "parent_contract_id": contract.parent_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        call_asset = system_asset(
            name=f"_tool_call_{contract.id}",
            content=call_content,
            created_by=contract.id,
            promptable=False,
        )

        ok = False
        result = ""
        error = ""
        if not isinstance(tool_name, str) or not tool_name:
            error = "tool action missing string payload.name"
        elif tool_name not in contract.tool_scope:
            error = f"tool '{tool_name}' is not in contract.tool_scope"
        elif self._tools is None:
            error = "no ToolRegistry configured"
        else:
            try:
                result = self._tools.run(tool_name, args if isinstance(args, dict) else {})
                ok = True
            except Exception as e:  # pragma: no cover - exact handler errors vary
                error = str(e)

        obs_name = contract.outputs[0] if contract.outputs else f"_tool_obs_{contract.id}"
        obs_content = json.dumps(
            {
                "ok": ok,
                "tool": tool_name,
                "result": result,
                "error": error,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        obs_asset = system_asset(
            name=obs_name,
            content=obs_content,
            created_by=contract.id,
            source_uri=f"tool://{tool_name}" if isinstance(tool_name, str) else "",
        )

        self._store.add_asset(sign_asset(call_asset))
        self._store.add_asset(sign_asset(obs_asset))
        self._add_trace(
            contract.id,
            "tool_executed",
            accepted_fragments=[call_asset.id, obs_asset.id],
            accepted_asset_names=[call_asset.name, obs_asset.name],
            authority_result="accepted" if ok else "rejected",
            relation_type="tool",
            relation_target=tool_name if isinstance(tool_name, str) else None,
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
        if method_assets:
            existing = {asset.id for asset in self._method_context.get(parent_id, [])}
            for asset in method_assets:
                if asset.id not in existing:
                    self._method_context.setdefault(parent_id, []).append(asset)
                    existing.add(asset.id)

        self._suspended.discard(parent_id)
        self._add_trace(
            parent_id,
            "method_resumed",
            disclosed_assets=[asset.id for asset in method_assets],
            relation_type=method_payload(contract).get("method"),
            relation_target=contract.id,
            budget_remaining=self._budget.get(parent_id, 0),
        )

        expanded = False
        method_type = method_payload(contract).get("method")
        if self._method_registry is not None and isinstance(method_type, str):
            handler = self._method_registry.get(method_type)
            if handler is not None and handler.can_handle(method_type):
                completion = getattr(handler, "handle_completion", None)
                if callable(completion):
                    expanded = completion(_make_runtime(self), contract, method_assets)
        if not expanded:
            self._expand_plan_result(contract, method_assets)

    def _expand_plan_result(
        self,
        method_contract: Contract,
        method_assets: list[Asset],
    ) -> None:
        if method_payload(method_contract).get("method") != "plan":
            return

        parent_id = method_contract.parent_id

        # --- Fail-closed: if parent_id is set but parent is not in store,
        #     do NOT expand at all (no validation = error, not no-validation). ---
        parent_contract: Contract | None = None
        if parent_id is not None:
            parent_contract = self._store.get_contract(parent_id)
            if parent_contract is None:
                self._add_trace(
                    parent_id,
                    "containment_rejected",
                    relation_type="plan",
                    relation_target="parent_not_found",
                    rejected_fragments=[
                        "[rejected] parent_not_found: "
                        f"parent contract {parent_id} not in store — "
                        "plan expansion abort (fail-closed)"
                    ],
                    authority_result="rejected",
                    budget_remaining=0,
                )
                return

        # Compute parent's disclosure scope for input/activation containment.
        allowed_input_names: set[str] | None = None
        parent_budget_remaining: int | None = None
        if parent_contract is not None:
            scope = compute_disclosure(parent_contract, self._store)
            allowed_input_names = {a.name for a in scope}
            parent_budget_remaining = self._resolve_budget(parent_contract)

        created: list[str] = []
        for asset in method_assets:
            if not asset.name.startswith("_plan_result_"):
                continue
            children, rejections = contracts_from_plan_asset(
                asset,
                parent_id,
                parent_contract=parent_contract,
                allowed_input_names=allowed_input_names,
                parent_budget_remaining=parent_budget_remaining,
            )
            for child in children:
                if self._store.get_contract(child.id) is None:
                    self.add_contract(child)
                    created.append(child.id)
            for entry in rejections:
                self._add_trace(
                    parent_id,
                    "containment_rejected",
                    relation_type="plan",
                    relation_target=(
                        f"{entry.get('child_name','?')}:{entry.get('field','?')}"
                    ),
                    rejected_fragments=[
                        f"[{entry.get('action','rejected')}] "
                        f"{entry.get('field','?')}: {entry.get('reason','')}"
                    ],
                    authority_result=entry.get("action", "rejected"),
                    budget_remaining=self._budget.get(parent_id, 0),
                )

        if created and parent_id is not None:
            self._add_trace(
                parent_id,
                "contracts_expanded",
                relation_type="plan",
                relation_target=",".join(created),
                budget_remaining=self._budget.get(parent_id, 0),
            )


    # ── State persistence / recovery ──────────────────────────────────

    def save_state(self) -> dict:
        """Serialize engine runtime state for recovery."""
        return {
            "budget": dict(self._budget),
            "completed": list(self._completed),
            "suspended": list(self._suspended),
            "method_scheduled": list(self._method_scheduled),
            "method_context": {
                k: [a.id for a in v] for k, v in self._method_context.items()
            },
            "label_context": {
                k: [a.id for a in v] for k, v in self._label_context.items()
            },
            "contract_last_entry": dict(self._contract_last_entry),
        }

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
        engine = cls(store, worker, trace_store, labels, tools, method_registry,
                     context_size_limit=context_size_limit)
        engine._budget = state["budget"]
        engine._completed = set(state["completed"])
        engine._suspended = set(state["suspended"])
        engine._method_scheduled = set(state["method_scheduled"])
        engine._method_context = {
            k: [a for aid in ids if (a := store.get_asset(aid)) is not None]
            for k, ids in state["method_context"].items()
        }
        engine._label_context = {
            k: [a for aid in ids if (a := store.get_asset(aid)) is not None]
            for k, ids in state["label_context"].items()
        }
        engine._contract_last_entry = state["contract_last_entry"]
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
        engine = cls(store, worker, trace_store, labels, tools, method_registry,
                     context_size_limit=context_size_limit)

        # Count activations and budget consumption per contract for budget derivation
        activation_counts: dict[str, int] = {}

        for entry in trace_store.get_all():
            cid = entry.contract_id

            if entry.event_type == "activation":
                activation_counts[cid] = activation_counts.get(cid, 0) + 1

            elif entry.event_type == "budget_consumed":
                activation_counts[cid] = activation_counts.get(cid, 0) + 1

            elif entry.event_type == "method_scheduled":
                engine._suspended.add(cid)
                if entry.relation_target:
                    engine._method_scheduled.add(entry.relation_target)

            elif entry.event_type == "complete":
                engine._completed.add(cid)
                engine._suspended.discard(cid)

            elif entry.event_type == "method_resumed":
                engine._suspended.discard(cid)
                # Reconstruct method context from disclosed_assets
                assets: list[Asset] = []
                for aid in entry.disclosed_assets:
                    asset = store.get_asset(aid)
                    if asset is not None:
                        assets.append(asset)
                if assets:
                    engine._method_context.setdefault(cid, []).extend(assets)

            elif entry.event_type == "label_resolved":
                # Reconstruct label context from disclosed_assets
                assets = []
                for aid in entry.disclosed_assets:
                    asset = store.get_asset(aid)
                    if asset is not None:
                        assets.append(asset)
                if assets:
                    engine._label_context[cid] = assets

            # Track last entry per contract for contract_last_entry
            engine._contract_last_entry[cid] = entry.id

        # Derive budget from contracts and activation counts
        for contract in store.get_all_contracts():
            cid = contract.id
            initial = max(contract.budget, 1)
            consumed = activation_counts.get(cid, 0)
            engine._budget[cid] = max(0, initial - consumed)

        return engine


def _make_runtime(engine: Engine) -> MethodRuntime:
    """Create a MethodRuntime from an Engine instance."""
    return MethodRuntime(
        store=engine._store,
        trace=engine._trace,
        budget=engine._budget,
        tools=engine._tools,
        suspended=engine._suspended,
        method_scheduled=engine._method_scheduled,
    )
