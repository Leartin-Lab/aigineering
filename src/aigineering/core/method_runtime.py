"""MethodRuntime — constrained interface for method handlers (G7).

Method handlers receive this instead of the full Engine. This enforces the
G7 gate: handlers must not access Engine private state (``_store``,
``_budget``, ``_add_trace``, ``_tools``, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.methods import (
    method_context_content,
    method_contract,
    system_asset,
)
from aigineering.core.provenance import sign_asset

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, Contract, Candidate
    from aigineering.protocol.actions import WorkerAction
    from aigineering.core.tools import ToolRegistry
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol


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
        trace: TraceStoreProtocol,
        budget: dict[str, int],
        tools: ToolRegistry | None = None,
        suspended: set[str] | None = None,
        method_scheduled: set[str] | None = None,
    ) -> None:
        self._store = store
        self._trace = trace
        self._budget = budget
        self._tools = tools
        self._suspended: set[str] = suspended if suspended is not None else set()
        self._method_scheduled: set[str] = (
            method_scheduled if method_scheduled is not None else set()
        )

    # -- Contract management -------------------------------------------------

    def add_contract(self, contract: Contract) -> None:
        """Add a contract to the store and initialize its budget.

        This is the single entry point for creating new contracts during
        method handling.  It replaces direct ``engine._store.add_contract()``
        and ``engine._budget[cid] = ...`` patterns.
        """
        self._store.add_contract(contract)
        self._budget[contract.id] = max(contract.budget, 1)

    def get_contract(self, contract_id: str) -> Contract | None:
        """Return the contract with *contract_id*, or ``None``."""
        return self._store.get_contract(contract_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        """Return all assets matching *name*."""
        return self._store.get_assets_by_name(name)

    @property
    def store(self) -> StoreProtocol:
        """The underlying store — for passing to disclosure computation."""
        return self._store

    # -- System asset minting ------------------------------------------------

    def mint_system_asset(
        self,
        name: str,
        content: str,
        created_by: str,
        promptable: bool = True,
        source_uri: str = "",
    ) -> Asset:
        """Create and sign a system asset, then add it to the store.

        Replaces direct ``engine._store.add_asset(sign_asset(...))`` patterns.
        """
        asset = system_asset(
            name=name,
            content=content,
            created_by=created_by,
            promptable=promptable,
            source_uri=source_uri,
        )
        signed = sign_asset(asset)
        self._store.add_asset(signed)
        return signed

    # -- Budget --------------------------------------------------------------

    def resolve_budget(self, contract_id: str) -> int:
        """Return the remaining budget for *contract_id*."""
        return self._budget.get(contract_id, 0)

    # -- Trace ---------------------------------------------------------------

    def append_trace(self, contract_id: str, event_type: str, **kwargs: object) -> None:
        """Append a trace entry for *contract_id*.

        Replaces direct ``engine._add_trace(...)`` calls.
        """
        from aigineering.core.trace import create_entry

        entry = create_entry(contract_id=contract_id, event_type=event_type, **kwargs)
        self._trace.append(entry)

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
        signed_ctx = sign_asset(ctx)
        self._store.add_asset(signed_ctx)

        self._method_scheduled.add(child.id)

        # Trace the scheduling
        self.append_trace(
            parent_contract.id,
            "method_scheduled",
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

    # -- Budget consumption --------------------------------------------------

    def consume_budget(self, contract_id: str, amount: int = 1) -> int:
        """Consume *amount* from the budget of *contract_id*. Returns remaining."""
        current = self._budget.get(contract_id, 0)
        remaining = max(0, current - amount)
        self._budget[contract_id] = remaining
        return remaining
