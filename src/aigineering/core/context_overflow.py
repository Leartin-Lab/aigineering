"""Context overflow detection for Engine scheduling."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.core.methods import system_asset
from aigineering.protocol.types import Asset, Contract

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
