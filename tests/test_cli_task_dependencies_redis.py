"""Actual-Redis relay acceptance for signed prerequisite facts."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


@pytest.fixture
def live_dependency_workspace(monkeypatch, tmp_path):
    url = os.getenv("AIG_REDIS_TEST_URL")
    if not url:
        pytest.skip("set AIG_REDIS_TEST_URL for actual Redis dependency acceptance")
    monkeypatch.setenv("AIGINEERING_REDIS_URL", url)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    initialized = runner.invoke(cli, ["domain", "init", "--json"])
    assert initialized.exit_code == 0, initialized.output
    target_result = runner.invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "dependent",
            "--description",
            "Wait",
            "--output",
            "result",
            "--json",
        ],
    )
    assert target_result.exit_code == 0, target_result.output
    prerequisite_result = runner.invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "prerequisite",
            "--description",
            "Prepare",
            "--output",
            "source",
            "--json",
        ],
    )
    assert prerequisite_result.exit_code == 0, prerequisite_result.output
    return (
        runner,
        json.loads(target_result.output)["contract_id"],
        json.loads(prerequisite_result.output)["contract_id"],
        tmp_path,
        url,
    )


def test_dependency_fact_relays_for_both_endpoints_and_survives_rebuild(
    live_dependency_workspace, monkeypatch
):
    runner, target, prerequisite, workspace, url = live_dependency_workspace
    linked = runner.invoke(
        cli,
        [
            "task",
            "link",
            target,
            "--depends-on",
            prerequisite,
            "--reason",
            "wait for prerequisite",
            "--idempotency-key",
            "redis-dep-link",
            "--json",
        ],
    )
    assert linked.exit_code == 0, linked.output
    constraint_id = json.loads(linked.output)["data"]["constraint_id"]

    target_events = runner.invoke(cli, ["task", "events", target, "--json"])
    prerequisite_events = runner.invoke(cli, ["task", "events", prerequisite, "--json"])
    assert target_events.exit_code == prerequisite_events.exit_code == 0
    before_target = json.loads(target_events.output)["data"]
    before_prerequisite = json.loads(prerequisite_events.output)["data"]
    link_event = next(
        item for item in before_target if item["record_id"] == constraint_id
    )
    assert target in link_event["contract_ids"]
    assert prerequisite in link_event["contract_ids"]
    assert any(item["record_id"] == constraint_id for item in before_prerequisite)

    unavailable = runner.invoke(
        cli,
        [
            "task",
            "link",
            target,
            "--depends-on",
            prerequisite,
            "--reason",
            "must fail before a second write",
            "--json",
        ],
    )
    # The configured test Redis is healthy here; this command reaches policy and
    # rejects the duplicate without creating another durable placement.
    assert unavailable.exit_code == 1
    assert json.loads(unavailable.output)["ok"] is False

    rebuilt = runner.invoke(cli, ["redis", "rebuild", "--json"])
    assert rebuilt.exit_code == 0, rebuilt.output
    target_after = runner.invoke(cli, ["task", "events", target, "--json"])
    prerequisite_after = runner.invoke(cli, ["task", "events", prerequisite, "--json"])
    assert json.loads(target_after.output)["data"] == before_target
    assert json.loads(prerequisite_after.output)["data"] == before_prerequisite

    monkeypatch.setenv("AIGINEERING_REDIS_URL", "redis://127.0.0.1:1/0")
    failed = runner.invoke(
        cli,
        [
            "task",
            "link",
            target,
            "--depends-on",
            prerequisite,
            "--reason",
            "Redis is required",
            "--json",
        ],
    )
    assert failed.exit_code == 1
    assert json.loads(failed.output)["error"] == "redis_unavailable"
    store = SQLiteStore.snapshot_from_path(workspace / ".aig" / "store.db")
    try:
        assert (
            len(store.scan_runtime_records(record_type="claim.constraint.placed")) == 1
        )
    finally:
        store.close()
    monkeypatch.setenv("AIGINEERING_REDIS_URL", url)
