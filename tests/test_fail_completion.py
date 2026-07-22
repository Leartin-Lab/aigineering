"""Fail completion remains explicit when its Candidate publisher is absent."""

from __future__ import annotations

import json

from aigineering.plugins.fail_completion import FailCompletionPlugin
from aigineering.protocol.types import Contract


class _RuntimeWithoutPublisher:
    def __init__(self, parent: Contract) -> None:
        self.parent = parent
        self.rejections: list[str] = []
        self.failed: list[str] = []
        self.traces: list[tuple[str, str]] = []

    def get_contract(self, contract_id):
        return self.parent if contract_id == self.parent.id else None

    def get_assets_by_name(self, name):
        del name
        return []

    def can_publish_candidates(self, plugin_id):
        del plugin_id
        return False

    def record_rejection(self, contract_id, reason, **kwargs):
        del contract_id, kwargs
        self.rejections.append(reason)

    def append_trace(self, contract_id, event_type, **kwargs):
        del kwargs
        self.traces.append((contract_id, event_type))

    def resolve_budget(self, contract_id):
        del contract_id
        return 1

    def fail_contract(self, contract, **kwargs):
        del kwargs
        self.failed.append(contract.id)
        return True


def test_fail_completion_without_publisher_records_rejection_and_closes_parent():
    parent = Contract(id="task:parent", outputs=("report",), budget=1)
    child = Contract(
        id="task:fail",
        parent_id=parent.id,
        origin="system",
        description=json.dumps({"method": "fail", "payload": {"reason": "no data"}}),
    )
    runtime = _RuntimeWithoutPublisher(parent)

    assert FailCompletionPlugin().handle_completion(runtime, child, []) is True
    assert runtime.failed == [parent.id]
    assert runtime.rejections == [
        "failure report requires an authenticated Candidate publisher"
    ]
    assert runtime.traces == [(parent.id, "fail_reported")]
