"""Context overflow detection and orchestration for Engine scheduling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigineering.core.methods import system_asset
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Asset, Candidate, Contract

if TYPE_CHECKING:
    from aigineering.core.runtime_ingress import RuntimeIngress
    from aigineering.core.trace_manager import TraceManager

_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class OverflowResult:
    """Structured context overflow details."""

    estimated_tokens: int
    limit: int


class ContextOverflowHandler:
    """Detect context overflow and create diagnostic assets."""

    def __init__(self, context_size_limit: int | None = None) -> None:
        self._limit = context_size_limit

    @property
    def limit(self) -> int | None:
        """Return the configured token limit."""
        return self._limit

    def estimate_tokens(self, scope: list[Asset]) -> int:
        """Estimate token count from asset content length."""
        total_chars = sum(len(asset.content) for asset in scope)
        return total_chars // _CHARS_PER_TOKEN

    def check_overflow(
        self, contract: Contract, scope: list[Asset]
    ) -> OverflowResult | None:
        """Return overflow details when a non-system contract exceeds the limit."""
        if contract.origin == "system" or self._limit is None:
            return None
        estimated = self.estimate_tokens(scope)
        if estimated <= self._limit:
            return None
        return OverflowResult(estimated_tokens=estimated, limit=self._limit)

    def create_report_asset(self, contract_id: str, overflow: OverflowResult) -> Asset:
        """Create a context-overflow diagnostic asset."""
        return system_asset(
            name="_context_overflow_report_",
            content=(
                f"Context overflow: {overflow.estimated_tokens} tokens "
                f"exceeds limit {overflow.limit}."
            ),
            created_by=contract_id,
        )


class ContextOverflowOrchestrator:
    """Orchestrate overflow detection, tracing, asset ingestion, and replan dispatch.

    The pure detection and asset creation is delegated to *overflow_handler*.
    This class owns the Engine-facing orchestration: trace recording, runtime
    ingress for the diagnostic asset, and dispatching a replan method action.
    """

    def __init__(
        self,
        overflow_handler: ContextOverflowHandler,
        trace_manager: TraceManager,
        ingress: RuntimeIngress,
    ) -> None:
        self._overflow_handler = overflow_handler
        self._trace_mgr = trace_manager
        self._ingress = ingress

    def handle_overflow(
        self,
        contract: Contract,
        scope: list[Asset],
        *,
        budget_remaining: int,
        add_trace: Callable[..., None],
        dispatch_method: Callable[..., None],
    ) -> bool:
        """Check for context overflow and orchestrate the response if detected.

        Parameters
        ----------
        contract:
            The contract whose disclosure scope is being checked.
        scope:
            The computed disclosure scope (list of assets).
        budget_remaining:
            Snapshot of the current budget remaining for trace attribution.
        add_trace:
            Engine trace callback with signature
            ``(contract_id, event_type, **kwargs) -> None``.
        dispatch_method:
            Engine method dispatch callback with signature
            ``(contract, action, candidate) -> None``.

        Returns
        -------
        bool
            ``True`` when overflow was detected and handled; ``False`` otherwise.
        """
        overflow = self._overflow_handler.check_overflow(contract, scope)
        if overflow is None:
            return False

        # Record overflow as trace event and diagnostic asset.
        # The replan is dispatched via the normal method ingress
        # (_dispatch_method → ReplanMethodHandler), NOT via Engine
        # fabricating a worker candidate.  The worker_id prefix
        # "runtime:" marks this as a kernel-generated method trigger.
        add_trace(
            contract.id,
            "context_overflow",
            disclosed_assets=[a.id for a in scope],
            relation_type="replan",
            relation_target="context_size_exceeded",
            rejected_fragments=[
                f"[replan_recommended] context size {overflow.estimated_tokens} "
                f"exceeds limit {overflow.limit} — replan recommended"
            ],
            budget_remaining=budget_remaining,
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
        dispatch_method(contract, action, candidate)
        return True
