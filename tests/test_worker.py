"""Tests for Worker protocol."""

from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import Worker
from aigineering.protocol.types import Contract


def test_mock_worker_satisfies_worker_protocol():
    worker = MockWorker()
    assert isinstance(worker, Worker)


def test_worker_returns_candidate_not_asset():
    worker = MockWorker({"task": "result: ok"})
    candidate = worker.invoke(Contract(id="c1", name="task"), [])

    assert candidate.worker_id == "mock_worker"
    assert candidate.raw_output == "result: ok"
