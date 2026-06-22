"""Per-contract budget tracking for the ACM boundary loop."""

from __future__ import annotations


class BudgetManager:
    """Manage per-contract budget initialization, consumption, and snapshots."""

    def __init__(self) -> None:
        self._budget: dict[str, int] = {}

    def initialize(self, contract_id: str, budget: int) -> int:
        """Initialize a contract budget if absent and return remaining budget."""
        if contract_id not in self._budget:
            self._budget[contract_id] = max(budget, 1)
        return self._budget[contract_id]

    def get_remaining(self, contract_id: str) -> int:
        """Return remaining budget for a contract, or zero if unknown."""
        return self._budget.get(contract_id, 0)

    def consume(self, contract_id: str, amount: int = 1) -> int:
        """Consume budget and return the remaining amount."""
        remaining = self._budget.get(contract_id, 0)
        self._budget[contract_id] = max(0, remaining - amount)
        return self._budget[contract_id]

    def get_all(self) -> dict[str, int]:
        """Return a copy of all tracked budgets."""
        return dict(self._budget)

    def restore(self, budget: dict[str, int]) -> None:
        """Replace all tracked budgets."""
        self._budget = dict(budget)
