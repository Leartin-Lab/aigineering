"""Worker package for contract execution context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerPackage:
    """Bundles contract context and disclosed assets for worker invocation."""

    contract_id: str
    contract: dict[str, Any]
    disclosed_assets: tuple[dict[str, Any], ...]
    method_context_assets: tuple[dict[str, Any], ...]
    tool_scope: tuple[str, ...]
    budget_remaining: int
    capability_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "disclosed_assets", tuple(self.disclosed_assets))
        object.__setattr__(self, "method_context_assets", tuple(self.method_context_assets))
        object.__setattr__(self, "tool_scope", tuple(self.tool_scope))
        object.__setattr__(self, "capability_requirements", tuple(self.capability_requirements))

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        d: dict[str, object] = {
            "contract_id": self.contract_id,
            "contract": self.contract,
            "disclosed_assets": list(self.disclosed_assets),
            "method_context_assets": list(self.method_context_assets),
            "tool_scope": list(self.tool_scope),
            "budget_remaining": self.budget_remaining,
            "capability_requirements": list(self.capability_requirements),
        }
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> WorkerPackage:
        """Deserialize from a JSON string."""
        d = json.loads(data)
        return cls(
            contract_id=d["contract_id"],
            contract=d["contract"],
            disclosed_assets=tuple(dict(a) for a in d["disclosed_assets"]),
            method_context_assets=tuple(dict(a) for a in d["method_context_assets"]),
            tool_scope=tuple(d["tool_scope"]),
            budget_remaining=int(d["budget_remaining"]),
            capability_requirements=tuple(d.get("capability_requirements", ())),
        )
