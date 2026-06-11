"""Candidate envelope for worker responses with routing metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class CandidateEnvelope:
    """Worker response with contract routing and optional claim identity."""

    contract_id: str
    worker_id: str
    raw_output: str
    claim_id: str = ""
    parsed_action: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ValueError("contract_id is required")
        if not self.worker_id:
            raise ValueError("worker_id is required")
        if not self.raw_output:
            raise ValueError("raw_output is required")

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        d: dict[str, object] = {
            "contract_id": self.contract_id,
            "worker_id": self.worker_id,
            "claim_id": self.claim_id,
            "raw_output": self.raw_output,
            "parsed_action": self.parsed_action,
        }
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> CandidateEnvelope:
        """Deserialize from a JSON string.  Raises ValueError on missing required fields."""
        d = json.loads(data)

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
            claim_id=d.get("claim_id", ""),
            raw_output=raw_output,
            parsed_action=d.get("parsed_action"),
        )
