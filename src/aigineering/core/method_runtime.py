"""MethodRuntime — constrained interface for method handlers (G7).

Method handlers receive this instead of the full Engine. This enforces the
G7 gate: handlers must not access Engine private state (``_store``,
``_budget``, ``_add_trace``, ``_tools``, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.authority import _is_protected_name
from aigineering.core.methods import (
    method_context_content,
    method_contract,
    system_asset,
)

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, Contract, Candidate
    from aigineering.protocol.actions import WorkerAction
    from aigineering.core.tools import ToolRegistry
    from aigineering.core.store import StoreProtocol
    from aigineering.core.budget_manager import BudgetManager
    from aigineering.core.trace import TraceStoreProtocol
    from aigineering.core.trace_manager import TraceManager
    from aigineering.core.runtime_ingress import RuntimeIngress
    from aigineering.core.candidate_publisher import CandidatePublisherRegistry
    from aigineering.protocol.candidate import CandidateEffect
    from aigineering.core.commitment import CommitmentDecision


class MethodRuntime:
    """Constrained runtime interface exposed to :class:`MethodHandler` implementations.

    Handlers use this to schedule sub-contracts, mint system assets, append
    trace entries, resolve budgets, and access parent contract state — without
    ever touching Engine private members.

    Gate: G7 (MethodRuntime Boundary)
    """

    def __init__(
        self,
        store: StoreProtocol,
        trace: TraceManager | TraceStoreProtocol,
        budget: BudgetManager | dict[str, int],
        tools: ToolRegistry | None = None,
        suspended: set[str] | None = None,
        method_scheduled: set[str] | None = None,
        mcp_servers: dict[str, object] | None = None,
        ingress: RuntimeIngress | None = None,
        candidate_publishers: CandidatePublisherRegistry | None = None,
    ) -> None:
        self._store = store
        self._trace = _coerce_trace_manager(trace)
        self._budget = _coerce_budget_manager(budget)
        self._tools = tools
        self._mcp_servers: dict[str, object] = mcp_servers or {}
        self._suspended: set[str] = suspended if suspended is not None else set()
        self._method_scheduled: set[str] = (
            method_scheduled if method_scheduled is not None else set()
        )
        self._candidate_publishers = candidate_publishers
        if ingress is not None:
            self._ingress = ingress
        else:
            from aigineering.core.fact_reducer import FactReducer
            from aigineering.core.runtime_ingress import RuntimeIngress

            self._ingress = RuntimeIngress(
                store, self._trace.store, FactReducer(store, self._trace.store)
            )

    # -- Contract management -------------------------------------------------

    def add_contract(self, contract: Contract) -> None:
        """Add a contract to the store and initialize its budget.

        This is the single entry point for creating new contracts during
        method handling.  It replaces direct ``engine._store.add_contract()``
        and ``engine._budget[cid] = ...`` patterns.
        """
        self._ingress.accept_contract(contract)
        self._budget.initialize(contract.id, contract.budget)

    def publish_task_effects(
        self,
        plugin_id: str,
        effects: tuple[CandidateEffect, ...],
        *,
        idempotency_key: str,
        causal_parents: tuple[str, ...] = (),
    ) -> CommitmentDecision | None:
        """Publish task effects when an authenticated plugin actor is configured."""
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

    def can_publish_candidates(self, plugin_id: str) -> bool:
        """Whether an authenticated publisher exists for ``plugin_id``."""
        return (
            self._candidate_publishers is not None
            and self._candidate_publishers.get(plugin_id) is not None
        )

    def get_contract(self, contract_id: str) -> Contract | None:
        """Return the contract with *contract_id*, or ``None``."""
        return self._store.get_contract(contract_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        """Return all assets matching *name*."""
        return self._store.get_assets_by_name(name)

    # -- Disclosure ----------------------------------------------------------

    def compute_disclosure(self, contract: Contract) -> list[Asset]:
        """Compute the disclosure scope for *contract*.

        Wraps :func:`disclosure.compute_disclosure` with the runtime's store.
        """
        from aigineering.core.disclosure import compute_disclosure as _compute

        return _compute(contract, self._store)

    # -- System asset minting (authorized) -----------------------------------

    def mint_authorized_system_asset(
        self,
        method_contract: Contract,
        name: str,
        content: str,
        created_by: str,
        promptable: bool = True,
        source_uri: str = "",
        origin: str = "system",
        trust_tier: str = "system",
        minted_by: str = "engine",
    ) -> Asset:
        """Create and sign a system asset, then add it to the store.

        Validates that the *method_contract* has minting authority for
        *name* when *name* starts with a reserved runtime prefix.

        Replaces direct ``engine._store.add_asset(sign_asset(...))`` patterns.
        """
        if _is_protected_name(name):
            if name not in method_contract.minting_authority:
                raise ValueError(
                    f"Contract '{method_contract.id}' lacks minting authority "
                    f"for reserved name '{name}'. "
                    f"minting_authority={list(method_contract.minting_authority)!r}"
                )
        asset = system_asset(
            name=name,
            content=content,
            created_by=created_by,
            promptable=promptable,
            source_uri=source_uri,
            origin=origin,
            trust_tier=trust_tier,
            minted_by=minted_by,
        )
        return self._ingress.accept_asset(asset, source="method", allow_protected=True)

    # -- Budget --------------------------------------------------------------

    def resolve_budget(self, contract_id: str) -> int:
        """Return the remaining budget for *contract_id*."""
        return self._budget.get_remaining(contract_id)

    # -- Trace ---------------------------------------------------------------

    def append_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        """Append a trace entry for *contract_id*.

        Replaces direct ``engine._add_trace(...)`` calls.
        """
        self._trace.record(contract_id, event_type, **kwargs)

    def record_rejection(self, contract_id: str, reason: str, **kwargs: object) -> None:
        """Record a rejection entry in the trace."""
        self.append_trace(
            contract_id,
            "rejection",
            rejected_fragments=[reason],
            **kwargs,
        )

    # -- Method scheduling ---------------------------------------------------

    def schedule_method(
        self,
        parent_contract: Contract,
        action: WorkerAction,
        candidate: Candidate,
    ) -> Contract | None:
        """Schedule a method sub-contract for *parent_contract*.

        Creates a deterministic child contract via :func:`method_contract`,
        adds it to the store, and records a ``method_scheduled`` trace event.
        Returns the created child contract, or ``None`` if already scheduled.

        Replaces direct ``engine._schedule_method_contract(...)`` calls.
        """
        child = method_contract(parent_contract, action)
        if child.id in self._method_scheduled:
            return None

        self.add_contract(child)

        # Create method context asset
        ctx = system_asset(
            name=f"_method_ctx_{parent_contract.id}",
            content=method_context_content(parent_contract, action, child),
            created_by=parent_contract.id,
        )
        self.mint_authorized_system_asset(
            child,
            name=ctx.name,
            content=ctx.content,
            created_by=ctx.created_by,
            promptable=ctx.promptable,
            source_uri=ctx.source_uri,
        )

        self._method_scheduled.add(child.id)

        # Trace the scheduling
        self.append_trace(
            parent_contract.id,
            "method_scheduled",
            worker_id=candidate.worker_id,
            candidate_raw=candidate.raw_output,
            relation_type=action.type,
            relation_target=child.id,
            disclosed_assets=[ctx.id],
            budget_remaining=self.resolve_budget(parent_contract.id),
        )

        self._suspended.add(parent_contract.id)
        return child

    # -- Tool registry access ------------------------------------------------

    def get_tool_registry(self) -> ToolRegistry | None:
        """Return the tool registry, or ``None`` if no tools configured.

        This is a constrained accessor — handlers cannot mutate the registry.
        """
        return self._tools

    def get_mcp_servers(self) -> dict[str, object]:
        """Return the MCP server registry (server name → callable).

        Returns an empty dict when no MCP servers are configured.
        """
        return dict(self._mcp_servers)

    # -- Budget consumption --------------------------------------------------

    def consume_budget(self, contract_id: str, amount: int = 1) -> int:
        """Consume *amount* from the budget of *contract_id*. Returns remaining."""
        return self._budget.consume(contract_id, amount)

    def cancel_contract(
        self,
        contract: Contract,
        *,
        reason: str,
        relation_target: str = "",
    ) -> bool:
        """Record one terminal cancellation through the method runtime.

        Recovery is execution control, not a CLI-only state mutation.  A
        terminal event is therefore emitted through this constrained runtime
        API, with an idempotency check against durable trace history.
        """
        return self._record_terminal(
            contract,
            "cancelled",
            reason=reason,
            relation_type="recover",
            relation_target=relation_target,
        )

    def fail_contract(
        self,
        contract: Contract,
        *,
        reason: str,
        relation_target: str = "",
    ) -> bool:
        """Record one explicit failed terminal for unfinished parent work."""
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
        terminal_events = {"complete", "failed", "cancelled", "unreachable"}
        existing = self._trace.store.get_by_contract(contract.id)
        if any(entry.event_type in terminal_events for entry in existing):
            return False
        self.append_trace(
            contract.id,
            event_type,
            relation_type=relation_type,
            relation_target=relation_target or contract.id,
            rejected_fragments=[f"[{event_type}] {relation_type}: {reason}"],
            budget_remaining=self.resolve_budget(contract.id),
        )
        return True


def _coerce_budget_manager(budget: BudgetManager | dict[str, int]) -> BudgetManager:
    if hasattr(budget, "initialize") and hasattr(budget, "consume"):
        return budget  # type: ignore[return-value]

    from aigineering.core.budget_manager import BudgetManager

    manager = BudgetManager()
    manager.restore(dict(budget))
    return manager


def _coerce_trace_manager(trace: TraceManager | TraceStoreProtocol) -> TraceManager:
    if hasattr(trace, "record") and hasattr(trace, "get_all_last_entries"):
        return trace  # type: ignore[return-value]

    from aigineering.core.trace_manager import TraceManager

    return TraceManager(trace)  # type: ignore[arg-type]
