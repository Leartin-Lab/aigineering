"""Actual-Redis assignment event references and rebuild acceptance."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_worker_host


class _IdleWorker:
    worker_id = "worker:assignment-redis"

    def invoke(self, _contract, _disclosed_assets):
        raise AssertionError("assignment event smoke must not invoke a worker")


@pytest.fixture
def assignment_workspace(monkeypatch, tmp_path):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL for actual Redis assignment acceptance")
    monkeypatch.setenv("AIGINEERING_REDIS_URL", redis_url)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    initialized = runner.invoke(cli, ["domain", "init", "--json"])
    assert initialized.exit_code == 0, initialized.output
    created = runner.invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "assignment event target",
            "--description",
            "A fixture task for typed Worker references.",
            "--output",
            "result",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    contract_id = json.loads(created.output)["contract_id"]
    store = SQLiteStore(tmp_path / ".aig" / "store.db")
    host = ensure_local_worker_host(store, _IdleWorker())
    store.close()
    return runner, contract_id, host.worker_id, tmp_path


def test_assignment_worker_ids_relay_and_rebuild(assignment_workspace):
    runner, contract_id, worker_id, _workspace = assignment_workspace
    assigned = runner.invoke(
        cli,
        [
            "task",
            "assign",
            contract_id,
            worker_id,
            "--reason",
            "route the exact task",
            "--idempotency-key",
            "assignment-redis-smoke",
            "--json",
        ],
    )
    assert assigned.exit_code == 0, assigned.output
    assignment = json.loads(assigned.output)["data"]

    events = runner.invoke(cli, ["task", "events", contract_id, "--json"])
    assert events.exit_code == 0, events.output
    before = json.loads(events.output)["data"]
    event = next(row for row in before if row["record_id"] == assignment["record_id"])
    assert event["worker_ids"] == [worker_id]

    rebuilt = runner.invoke(cli, ["redis", "rebuild", "--json"])
    assert rebuilt.exit_code == 0, rebuilt.output
    after = runner.invoke(cli, ["task", "events", contract_id, "--json"])
    assert after.exit_code == 0, after.output
    assert json.loads(after.output)["data"] == before
