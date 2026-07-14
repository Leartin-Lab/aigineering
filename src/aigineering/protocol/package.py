"""Worker package for contract execution context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aigineering.core.ids import compute_content_hash

# Version 2 binds the selected WorkerProfile and registration revision into
# package identity. Older packages fail closed rather than silently losing
# routing evidence.
CURRENT_PROTOCOL_VERSION = 3


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
    claim_id: str = ""
    claim_epoch: int = 0
    lease_until: str = ""
    capability_requirements: tuple[str, ...] = ()
    worker_profile_id: str = ""
    worker_registration_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "disclosed_assets", tuple(self.disclosed_assets))
        object.__setattr__(
            self, "method_context_assets", tuple(self.method_context_assets)
        )
        if self.claim_epoch < 0:
            raise ValueError("claim_epoch must not be negative")
        object.__setattr__(self, "tool_scope", tuple(self.tool_scope))
        object.__setattr__(
            self, "capability_requirements", tuple(self.capability_requirements)
        )

        # Compute or verify package_id deterministically
        computed = self._compute_package_id()
        if not self.package_id:
            object.__setattr__(self, "package_id", computed)
        elif self.package_id != computed:
            raise ValueError(
                f"Package integrity check failed: provided package_id "
                f"{self.package_id} does not match computed {computed}"
            )

        # Fail closed on unsupported protocol version
        if self.protocol_version != CURRENT_PROTOCOL_VERSION:
            raise ValueError(
                f"Unsupported protocol version {self.protocol_version} "
                f"(current: {CURRENT_PROTOCOL_VERSION})"
            )

    def _compute_package_id(self) -> str:
        """Deterministic identity for contract context and disclosed inputs.

        Claim and lease fields are intentionally excluded to avoid circular
        dependency: the store binds an active claim to this package_id, and the
        candidate envelope carries both package_id and claim_id.
        """
        contract_hash = compute_content_hash(json.dumps(self.contract, sort_keys=True))
        disclosure_hash = compute_content_hash(
            json.dumps(list(self.disclosed_assets), sort_keys=True)
        )
        method_hash = compute_content_hash(
            json.dumps(list(self.method_context_assets), sort_keys=True)
        )
        tool_scope_hash = compute_content_hash(json.dumps(sorted(self.tool_scope)))
        payload = (
            f"v{self.protocol_version}|{self.contract_id}"
            f"|{contract_hash}|{disclosure_hash}|{method_hash}"
            f"|b{self.budget_remaining}|t{tool_scope_hash}"
            f"|c{','.join(sorted(self.capability_requirements))}"
            f"|p{self.worker_profile_id}"
            f"|r{self.worker_registration_version}"
        )
        return f"pkg:{compute_content_hash(payload)}"

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        d: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "package_id": self.package_id,
            "claim_id": self.claim_id,
            "claim_epoch": self.claim_epoch,
            "lease_until": self.lease_until,
            "contract_id": self.contract_id,
            "contract": self.contract,
            "disclosed_assets": list(self.disclosed_assets),
            "method_context_assets": list(self.method_context_assets),
            "tool_scope": list(self.tool_scope),
            "budget_remaining": self.budget_remaining,
            "capability_requirements": list(self.capability_requirements),
            "worker_profile_id": self.worker_profile_id,
            "worker_registration_version": self.worker_registration_version,
        }
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> WorkerPackage:
        """Deserialize from a JSON string. Fails closed on unknown version."""
        d = json.loads(data)
        version = d.get("protocol_version")
        if version is None:
            version = CURRENT_PROTOCOL_VERSION
        if version != CURRENT_PROTOCOL_VERSION:
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
            claim_id=d.get("claim_id", ""),
            claim_epoch=int(d.get("claim_epoch", 0)),
            lease_until=d.get("lease_until", ""),
            capability_requirements=tuple(d.get("capability_requirements", ())),
            worker_profile_id=d.get("worker_profile_id", ""),
            worker_registration_version=d.get("worker_registration_version", ""),
        )
