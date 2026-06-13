"""Worker package for contract execution context."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aigineering.core.ids import compute_content_hash

CURRENT_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class WorkerPackage:
    """Bundles contract context and disclosed assets for worker invocation.

    Versioned protocol object — unknown versions fail closed.
    """

    contract_id: str
    contract: dict[str, Any]
    disclosed_assets: tuple[dict[str, Any], ...]
    method_context_assets: tuple[dict[str, Any], ...]
    tool_scope: tuple[str, ...]
    budget_remaining: int
    protocol_version: int = CURRENT_PROTOCOL_VERSION
    package_id: str = ""
    capability_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "disclosed_assets", tuple(self.disclosed_assets))
        object.__setattr__(self, "method_context_assets", tuple(self.method_context_assets))
        object.__setattr__(self, "tool_scope", tuple(self.tool_scope))
        object.__setattr__(self, "capability_requirements", tuple(self.capability_requirements))

        # Compute package_id deterministically if not provided
        if not self.package_id:
            object.__setattr__(self, "package_id", self._compute_package_id())

        # Fail closed on unknown protocol version
        if self.protocol_version > CURRENT_PROTOCOL_VERSION:
            raise ValueError(
                f"Unsupported protocol version {self.protocol_version} "
                f"(current: {CURRENT_PROTOCOL_VERSION})"
            )

    def _compute_package_id(self) -> str:
        """Deterministic package identity from contract + disclosure hashes."""
        contract_hash = compute_content_hash(json.dumps(self.contract, sort_keys=True))
        disclosure_hash = compute_content_hash(
            json.dumps(list(self.disclosed_assets), sort_keys=True)
        )
        method_hash = compute_content_hash(
            json.dumps(list(self.method_context_assets), sort_keys=True)
        )
        payload = f"v{self.protocol_version}|{self.contract_id}|{contract_hash}|{disclosure_hash}|{method_hash}"
        return f"pkg:{compute_content_hash(payload)}"

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        d: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "package_id": self.package_id,
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
        """Deserialize from a JSON string. Fails closed on unknown version."""
        d = json.loads(data)
        version = d.get("protocol_version", 0)
        if version > CURRENT_PROTOCOL_VERSION:
            raise ValueError(
                f"Unsupported protocol version {version} (current: {CURRENT_PROTOCOL_VERSION})"
            )
        return cls(
            contract_id=d["contract_id"],
            contract=d["contract"],
            disclosed_assets=tuple(dict(a) for a in d["disclosed_assets"]),
            method_context_assets=tuple(dict(a) for a in d["method_context_assets"]),
            tool_scope=tuple(d["tool_scope"]),
            budget_remaining=int(d["budget_remaining"]),
            protocol_version=version,
            package_id=d.get("package_id", ""),
            capability_requirements=tuple(d.get("capability_requirements", ())),
        )
