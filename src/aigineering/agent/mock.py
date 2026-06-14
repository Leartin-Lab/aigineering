"""MockWorker — deterministic worker for testing the ACM boundary."""

from __future__ import annotations

from typing import Optional

from aigineering.protocol.types import Asset, Candidate, Contract


class MockWorker:
    _DEFAULT_PRESETS: dict[str, str] = {
        "build_report": (
            "final_report: According to Smith 2025, the results show "
            "significant improvement in thermal efficiency across all "
            "test conditions.\n"
            "citation_summary: Key finding from Smith 2025 demonstrates "
            "that thermal efficiency improved by 47%."
        ),
    }

    def __init__(
        self,
        preset_outputs: Optional[dict[str, str]] = None,
        worker_id: str = "mock_worker",
    ) -> None:
        self._outputs: dict[str, str] = dict(self._DEFAULT_PRESETS)
        if preset_outputs:
            self._outputs.update(preset_outputs)
        self._worker_id: str = worker_id

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def set_output(self, contract_name: str, raw_output: str) -> None:
        self._outputs[contract_name] = raw_output

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        raw_output: str = self._outputs.get(contract.name, "")
        return Candidate(
            worker_id=self.worker_id,
            raw_output=raw_output,
            parsed_action=None,
        )
