"""Actual Redis relay check for signed priority history."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


def test_priority_relay_and_rebuild(tmp_path, monkeypatch):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL for actual Redis priority acceptance")
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
            "priority relay target",
            "--description",
            "Fixture-backed task",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    contract_id = json.loads(created.output)["contract_id"]
    changed = runner.invoke(
        cli,
        [
            "task",
            "priority",
            contract_id,
            "-9",
            "--reason",
            "exercise required relay",
            "--idempotency-key",
            "priority-redis-1",
            "--json",
        ],
    )
    assert changed.exit_code == 0, changed.output
    item = json.loads(changed.output)["data"]
    assert item["priority"] == -9
    before = runner.invoke(cli, ["task", "priorities", contract_id, "--json"])
    assert before.exit_code == 0, before.output
    before_data = json.loads(before.output)["data"]
    assert before_data["head_priority_id"] == item["record_id"]
    events = runner.invoke(cli, ["task", "events", contract_id, "--json"])
    assert events.exit_code == 0, events.output
    before_events = json.loads(events.output)["data"]
    priority_event = next(
        record for record in before_events if record["record_id"] == item["record_id"]
    )
    assert priority_event["record_type"] == "task.priority.changed"
    assert priority_event["contract_ids"] == [contract_id]
    assert priority_event["worker_ids"] == []

    rebuilt = runner.invoke(cli, ["redis", "rebuild", "--json"])
    assert rebuilt.exit_code == 0, rebuilt.output
    after = runner.invoke(cli, ["task", "priorities", contract_id, "--json"])
    assert after.exit_code == 0, after.output
    assert json.loads(after.output)["data"] == before_data
    events_after = runner.invoke(cli, ["task", "events", contract_id, "--json"])
    assert events_after.exit_code == 0, events_after.output
    assert json.loads(events_after.output)["data"] == before_events

    store = SQLiteStore.snapshot_from_path(tmp_path / ".aig" / "store.db")
    try:
        facts = store.scan_runtime_records(record_type="task.priority.changed")
        assert len(facts) == 1
        assert facts[0][1].id == item["record_id"]
    finally:
        store.close()
