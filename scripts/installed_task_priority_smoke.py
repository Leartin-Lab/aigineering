"""Installed priority CLI and actual signed claim control fixtures.

No Worker is invoked and no business output or provider request is produced.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from installed_assignment_smoke import _REBUILD_SCRIPT, _SNAPSHOT_SCRIPT
from installed_smoke import json_command, run
from installed_worker_coordination_smoke import _python_json, _task


def installed_task_priority_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Check installed publication, immutable history, ordering and rebuild."""
    workspace = cwd / "installed-task-priority"
    workspace.mkdir(mode=0o700)
    database = workspace / ".aig" / "store.db"
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )

    def command(*args: str):
        return json_command(aig, ["-C", str(workspace), *args], cwd=cwd, env=local_env)

    def priority(*args: str) -> dict:
        result = command("task", "priority", *args)
        assert result["ok"] is True and type(result["revision"]) is int
        return result["data"]

    def history(contract_id: str) -> dict:
        result = command("task", "priorities", contract_id)
        assert result["ok"] is True and type(result["revision"]) is int
        return result["data"]

    def snapshot() -> dict:
        return _python_json(
            python, _SNAPSHOT_SCRIPT, [str(database)], cwd=cwd, env=local_env
        )

    def rejected(*args: str) -> None:
        before = snapshot()["record_ids"]
        result = subprocess.run(
            [str(aig), "-C", str(workspace), "task", "priority", *args, "--json"],
            cwd=cwd,
            env=local_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert json.loads(result.stdout)["ok"] is False
        assert snapshot()["record_ids"] == before

    command("domain", "init")
    low, high, held = (
        _task(aig, workspace, name, name + "_output", cwd=cwd, env=local_env)
        for name in ("priority-low", "priority-high", "priority-held")
    )
    targets = {low, high, held}
    default_order = [
        row["contract_id"]
        for row in command("task", "list")["data"]["tasks"]
        if row["contract_id"] in targets
    ]
    assert history(low) == {
        "contract_id": low,
        "priority": 0,
        "head_priority_id": None,
        "history": [],
    }
    rejected("unknown-priority-task", "10", "--reason", "unknown target")
    negative = priority(low, "-10", "--reason", "defer this fixture")
    original_high_args = (
        high,
        "80",
        "--reason",
        "urgent fixture",
        "--idempotency-key",
        "installed-priority-high",
    )
    original_high = priority(*original_high_args)
    original_held = priority(held, "100", "--reason", "urgent but held")
    facts = {low: negative, high: original_high, held: original_held}
    listed = command("task", "list", "--sort", "priority")["data"]["tasks"]
    relevant = [row for row in listed if row["contract_id"] in targets]
    assert [row["contract_id"] for row in relevant] == [held, high, low]
    assert [
        row["contract_id"]
        for row in command("task", "list")["data"]["tasks"]
        if row["contract_id"] in targets
    ] == default_order
    columns = command("task", "board", "--sort", "priority")["data"]["columns"]
    ready = [row for row in columns["ready"] if row["contract_id"] in targets]
    assert [row["contract_id"] for row in ready] == [held, high, low]
    for row in relevant + ready:
        fact = facts[row["contract_id"]]
        assert row["priority"] == fact["priority"]
        assert row["priority_fact_id"] == fact["record_id"]
    for target, fact in facts.items():
        for view in (
            command("task", "show", target)["data"]["task"],
            command("task", "status", target),
        ):
            assert view["priority"] == fact["priority"]
            assert view["priority_fact_id"] == fact["record_id"]
    command("task", "block", held, "--reason", "hold must beat priority")

    first = command("worker", "next", "--worker-id", "installed-priority-worker")
    assert first["contract_id"] == high
    before_claim = snapshot()["claims"][high]
    changed = priority(
        high,
        "90",
        "--previous",
        original_high["record_id"],
        "--reason",
        "advisory edit while claimed",
    )
    assert snapshot()["claims"][high] == before_claim
    cleared = priority(
        low,
        "0",
        "--previous",
        negative["record_id"],
        "--reason",
        "clear hint",
    )
    assert history(low)["head_priority_id"] == cleared["record_id"]
    assert [row["priority"] for row in history(low)["history"]] == [-10, 0]
    human_history = run(
        [str(aig), "-C", str(workspace), "task", "priorities", low],
        cwd=cwd,
        env=local_env,
    )
    assert cleared["record_id"] in human_history
    assert negative["record_id"] in human_history
    rejected(low, "20", "--reason", "missing predecessor")
    rejected(low, "20", "--previous", negative["record_id"], "--reason", "stale head")
    rejected(low, "0", "--previous", cleared["record_id"], "--reason", "no-op")

    before_replay = snapshot()["record_ids"]
    assert priority(*original_high_args)["record_id"] == original_high["record_id"]
    assert snapshot()["record_ids"] == before_replay
    command("task", "cancel", high, "--reason", "close fixture claim")
    rejected(
        high,
        "70",
        "--previous",
        changed["record_id"],
        "--reason",
        "terminal edit",
    )
    before_replay = snapshot()["record_ids"]
    assert priority(*original_high_args)["record_id"] == original_high["record_id"]
    assert snapshot()["record_ids"] == before_replay
    second = command("worker", "next", "--worker-id", "installed-priority-worker")
    assert second["contract_id"] == low
    assert snapshot()["claims"][held] is None

    history_before = {target: history(target) for target in (low, high, held)}
    events_before = {
        target: command("task", "events", target)["data"]
        for target in (low, high, held)
    }
    for target, task_history in history_before.items():
        event_by_id = {row["record_id"]: row for row in events_before[target]}
        for fact in task_history["history"]:
            event = event_by_id[fact["record_id"]]
            assert event["record_type"] == "task.priority.changed"
            assert event["contract_ids"] == [target]
            assert event["worker_ids"] == []
    before = snapshot()
    rebuild = _python_json(
        python, _REBUILD_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    assert rebuild["same_ids"] and rebuild["same_digest"]
    command("redis", "rebuild")
    assert snapshot() == before
    assert {target: history(target) for target in (low, high, held)} == history_before
    assert {
        target: command("task", "events", target)["data"]
        for target in (low, high, held)
    } == events_before
    return {
        "classification": "installed_signed_priority_and_claim_control_fixture",
        "direct_negative_integer": True,
        "default_and_clear_history": True,
        "invalid_intent_before_publication": True,
        "higher_eligible_claimed_first": True,
        "hold_beats_priority": True,
        "claimed_attempt_unchanged": True,
        "exact_replay_after_edit_and_terminal": True,
        "sqlite_and_redis_rebuild_equal": True,
        "priority_event_references_and_rebuild_equal": True,
        "nonzero_views_and_explicit_sort": True,
        "provider_requests": 0,
        "business_outputs": 0,
    }
