"""Worker protocol for candidate-producing execution environments."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aigineering.protocol.types import Asset, Candidate, Contract


@runtime_checkable
class Worker(Protocol):
    """Execution environment that returns candidates, never committed facts."""

    worker_id: str

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate: ...
