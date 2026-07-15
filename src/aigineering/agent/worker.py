"""Worker protocol for candidate-producing execution environments."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aigineering.protocol.types import Asset, Candidate, Contract


class WorkerExecutionError(ValueError):
    """Expected, safely reportable failure before Candidate production."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@runtime_checkable
class Worker(Protocol):
    """Execution environment that returns candidates, never committed facts."""

    worker_id: str

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate: ...
