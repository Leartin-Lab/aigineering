"""Actual Redis relay coverage for signed task-hold promotion."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


def test_task_promotion_events_survive_redis_rebuild(tmp_path, monkeypatch):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL for actual Redis promotion acceptance")
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
            "promotion relay target",
            "--description",
            "Fixture-backed blocked task",
            "--output",
            "promotion-result",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    contract_id = json.loads(created.output)["contract_id"]

    hold_ids = []
    for reason in ("first relay hold", "second relay hold"):
        blocked = runner.invoke(
            cli,
            ["task", "block", contract_id, "--reason", reason, "--json"],
        )
        assert blocked.exit_code == 0, blocked.output
        hold_ids.append(json.loads(blocked.output)["data"]["hold_id"])

    promoted = runner.invoke(
        cli,
        ["task", "promote", contract_id, "--reason", "release relay holds", "--json"],
    )
    assert promoted.exit_code == 0, promoted.output
    promoted_data = json.loads(promoted.output)["data"]
    assert promoted_data["hold_ids"] == sorted(hold_ids)
    expected_ids = {*promoted_data["release_record_ids"], promoted_data["record_id"]}

    events = runner.invoke(
        cli, ["task", "events", contract_id, "--limit", "10000", "--json"]
    )
    assert events.exit_code == 0, events.output
    before = json.loads(events.output)["data"]
    selected = {
        item["record_id"]: item for item in before if item["record_id"] in expected_ids
    }
    assert set(selected) == expected_ids
    assert selected[promoted_data["record_id"]]["record_type"] == "task.promoted"
    assert all(
        selected[record_id]["record_type"] == "claim.hold.released"
        for record_id in promoted_data["release_record_ids"]
    )
    assert all(
        contract_id in selected[item_id]["contract_ids"] for item_id in expected_ids
    )

    rebuilt = runner.invoke(cli, ["redis", "rebuild", "--json"])
    assert rebuilt.exit_code == 0, rebuilt.output
    events_after = runner.invoke(
        cli, ["task", "events", contract_id, "--limit", "10000", "--json"]
    )
    assert events_after.exit_code == 0, events_after.output
    assert json.loads(events_after.output)["data"] == before

    store = SQLiteStore.snapshot_from_path(tmp_path / ".aig" / "store.db")
    try:
        promoted_facts = store.scan_runtime_records(record_type="task.promoted")
        release_facts = store.scan_runtime_records(record_type="claim.hold.released")
        assert len(promoted_facts) == 1
        assert promoted_facts[0][1].id == promoted_data["record_id"]
        assert {record.id for _revision, record in release_facts} == set(
            promoted_data["release_record_ids"]
        )
    finally:
        store.close()
