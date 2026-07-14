"""Candidate envelope for worker responses with routing metadata.

Versioned protocol object — unknown versions fail closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from aigineering.core.ids import compute_content_hash

CURRENT_ENVELOPE_VERSION = 2


@dataclass(frozen=True)
class CandidateEnvelope:
    """Worker response with contract routing, claim identity, and idempotency."""

    contract_id: str
    worker_id: str
    raw_output: str
    protocol_version: int = CURRENT_ENVELOPE_VERSION
    package_id: str = ""
    claim_id: str = ""
    claim_epoch: int = 0
    idempotency_key: str = ""
    parsed_action: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ValueError("contract_id is required")
        if not self.worker_id:
            raise ValueError("worker_id is required")
        if not self.raw_output:
            raise ValueError("raw_output is required")
        if (
            not isinstance(self.protocol_version, int)
            or self.protocol_version != CURRENT_ENVELOPE_VERSION
        ):
            raise ValueError(
                f"Unsupported envelope protocol version {self.protocol_version} "
                f"(current: {CURRENT_ENVELOPE_VERSION})"
            )
        if self.claim_id and len(self.claim_id) > 256:
            raise ValueError("claim_id exceeds maximum length (256)")
        if self.claim_epoch < 0:
            raise ValueError("claim_epoch must not be negative")
        if self.idempotency_key and len(self.idempotency_key) > 256:
            raise ValueError("idempotency_key exceeds maximum length (256)")
        if self.package_id and not self.package_id.startswith("pkg:"):
            raise ValueError("package_id must start with 'pkg:'")

    @property
    def candidate_hash(self) -> str:
        """Deterministic hash of the candidate payload for integrity verification."""
        payload = json.dumps(
            {
                "protocol_version": self.protocol_version,
                "contract_id": self.contract_id,
                "worker_id": self.worker_id,
                "raw_output": self.raw_output,
                "claim_id": self.claim_id,
                "claim_epoch": self.claim_epoch,
                "idempotency_key": self.idempotency_key,
                "package_id": self.package_id,
                "parsed_action": self.parsed_action,
            },
            sort_keys=True,
        )
        return f"cand:{compute_content_hash(payload)}"

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        d: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "contract_id": self.contract_id,
            "worker_id": self.worker_id,
            "package_id": self.package_id,
            "claim_id": self.claim_id,
            "claim_epoch": self.claim_epoch,
            "idempotency_key": self.idempotency_key,
            "raw_output": self.raw_output,
            "parsed_action": self.parsed_action,
        }
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> CandidateEnvelope:
        """Deserialize from a JSON string. Fails closed on unknown version."""
        d = json.loads(data)

        version = d.get("protocol_version")
        if version is None:
            version = CURRENT_ENVELOPE_VERSION
        if not isinstance(version, int) or version != CURRENT_ENVELOPE_VERSION:
            raise ValueError(
                f"Unsupported envelope protocol version {version} "
                f"(current: {CURRENT_ENVELOPE_VERSION})"
            )

        contract_id = d.get("contract_id", "")
        worker_id = d.get("worker_id", "")
        raw_output = d.get("raw_output", "")

        if not contract_id:
            raise ValueError("contract_id is required")
        if not worker_id:
            raise ValueError("worker_id is required")
        if not raw_output:
            raise ValueError("raw_output is required")

        return cls(
            contract_id=contract_id,
            worker_id=worker_id,
            raw_output=raw_output,
            protocol_version=version,
            package_id=d.get("package_id", ""),
            claim_id=d.get("claim_id", ""),
            claim_epoch=int(d.get("claim_epoch", 0)),
            idempotency_key=d.get("idempotency_key", ""),
            parsed_action=d.get("parsed_action"),
        )
