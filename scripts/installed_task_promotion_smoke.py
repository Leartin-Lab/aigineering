"""Installed CLI checks for atomic task-hold promotion.

The fixture uses signed local Candidates and never invokes a Worker model or
creates a business output.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from installed_assignment_smoke import _REBUILD_SCRIPT, _SNAPSHOT_SCRIPT
from installed_smoke import json_command, run
from installed_worker_coordination_smoke import _python_json, _task


def installed_task_promotion_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise signed promotion, replay, partial results, and rebuild."""
    workspace = cwd / "installed-task-promotion"
    workspace.mkdir(mode=0o700)
    database = workspace / ".aig" / "store.db"
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )

    def command(*args: str) -> dict:
        return json_command(aig, ["-C", str(workspace), *args], cwd=cwd, env=local_env)

    def status(contract_id: str) -> dict:
        return json_command(
            aig,
            ["-C", str(workspace), "task", "status", contract_id],
            cwd=cwd,
            env=local_env,
        )

    def promote_failure(*contract_ids: str) -> dict:
        completed = subprocess.run(
            [
                str(aig),
                "-C",
                str(workspace),
                "task",
                "promote",
                *contract_ids,
                "--reason",
                "Installed multi-target promotion fixture.",
                "--json",
            ],
            cwd=cwd,
            env=local_env,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            raise RuntimeError(
                "multi-target promotion unexpectedly hid a failed target"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "multi-target promotion failure did not return its JSON result"
            ) from exc

    def snapshot() -> dict:
        return _python_json(
            python, _SNAPSHOT_SCRIPT, [str(database)], cwd=cwd, env=local_env
        )

    command("domain", "init")

    atomic = _task(
        aig,
        workspace,
        "promotion-atomic",
        "promotion-atomic-output",
        cwd=cwd,
        env=local_env,
    )
    first_hold = command("task", "block", atomic, "--reason", "First operator hold")[
        "data"
    ]["hold_id"]
    second_hold = command("task", "block", atomic, "--reason", "Second operator hold")[
        "data"
    ]["hold_id"]
    worker_id = "installed-promotion-worker"
    before_claim = command(
        "worker", "next", "--worker-id", worker_id, "--contract", atomic
    )
    if before_claim is not None:
        raise RuntimeError(f"held task was claimed before promotion: {before_claim}")
    if snapshot()["claims"].get(atomic) is not None:
        raise RuntimeError("held task acquired a claim before promotion")

    original_arguments = [
        "task",
        "promote",
        atomic,
        "--hold",
        first_hold,
        "--hold",
        second_hold,
        "--reason",
        "Release the complete reviewed hold set.",
        "--idempotency-key",
        "installed-promotion-exact-replay",
    ]
    promoted = command(*original_arguments)["data"]
    if (
        promoted.get("record_type") != "task.promoted"
        or promoted.get("contract_id") != atomic
        or promoted.get("hold_ids") != sorted([first_hold, second_hold])
        or len(promoted.get("release_record_ids", [])) != 2
        or promoted.get("remaining_hold_ids") != []
        or promoted.get("status") != "ready"
    ):
        raise RuntimeError(f"promotion did not release the exact hold set: {promoted}")

    history_before_later_hold = command("task", "promotions", atomic)["data"]
    exact_replay = command(*original_arguments)["data"]
    if exact_replay != promoted:
        raise RuntimeError("exact promotion replay changed its durable result")
    if len(history_before_later_hold.get("history", [])) != 1:
        raise RuntimeError("exact promotion replay appended another history item")
    readable_history = run(
        [str(aig), "-C", str(workspace), "task", "promotions", atomic],
        cwd=cwd,
        env=local_env,
    )
    if promoted["record_id"] not in readable_history:
        raise RuntimeError("human promotion history omitted its durable record")

    later_hold = command(
        "task", "block", atomic, "--reason", "A later hold must survive replay"
    )["data"]["hold_id"]
    replay_after_later_hold = command(*original_arguments)["data"]
    if (
        replay_after_later_hold.get("record_id") != promoted.get("record_id")
        or replay_after_later_hold.get("release_record_ids")
        != promoted.get("release_record_ids")
        or replay_after_later_hold.get("remaining_hold_ids") != [later_hold]
        or replay_after_later_hold.get("status") != "blocked"
    ):
        raise RuntimeError(
            "exact promotion replay released or hid a later hold: "
            f"{replay_after_later_hold}"
        )
    if len(command("task", "promotions", atomic)["data"]["history"]) != 1:
        raise RuntimeError("replay after a later hold added a promotion fact")

    prerequisite = _task(
        aig,
        workspace,
        "promotion-prerequisite",
        "promotion-prerequisite-output",
        cwd=cwd,
        env=local_env,
    )
    dependent = _task(
        aig,
        workspace,
        "promotion-dependent",
        "promotion-dependent-output",
        cwd=cwd,
        env=local_env,
    )
    command(
        "task",
        "link",
        dependent,
        "--depends-on",
        prerequisite,
        "--reason",
        "Keep the prerequisite blocker after promotion.",
    )
    command("task", "block", dependent, "--reason", "Release only the hold blocker")

    activation_created = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            "promotion-activation-blocked",
            "--description",
            "Remain blocked on an unavailable activation asset.",
            "--activation",
            "promotion-missing-gate",
            "--budget",
            "2",
            "--output",
            "promotion-activation-output",
        ],
        cwd=cwd,
        env=local_env,
    )
    activation_blocked = activation_created["contract_id"]
    command(
        "task", "block", activation_blocked, "--reason", "Release only the hold blocker"
    )

    partial = promote_failure(
        dependent, activation_blocked, "task:unknown-promotion-target"
    )
    result_data = partial.get("data", {})
    results = result_data.get("results", [])
    if (
        partial.get("ok") is not False
        or result_data.get("success_count") != 2
        or result_data.get("failed_count") != 1
        or len(results) != 3
        or [row.get("ok") for row in results] != [True, True, False]
    ):
        raise RuntimeError(
            f"multi-target promotion lost its per-target outcomes: {partial}"
        )
    dependent_status = status(dependent)
    activation_status = status(activation_blocked)
    if dependent_status.get("status") != "blocked" or not any(
        blocker.startswith("prerequisite:")
        for blocker in dependent_status.get("blockers", [])
    ):
        raise RuntimeError(
            f"promotion cleared a prerequisite blocker: {dependent_status}"
        )
    if activation_status.get("status") != "blocked" or not any(
        blocker == "missing_asset:promotion-missing-gate"
        for blocker in activation_status.get("blockers", [])
    ):
        raise RuntimeError(
            f"promotion cleared an activation blocker: {activation_status}"
        )
    for target in (dependent, activation_blocked):
        remaining = command("task", "holds", target)["data"]
        if any(item.get("active") for item in remaining):
            raise RuntimeError(
                f"promotion left a selected target hold open: {remaining}"
            )

    claimable = _task(
        aig,
        workspace,
        "promotion-claimable",
        "promotion-claimable-output",
        cwd=cwd,
        env=local_env,
    )
    claim_hold = command("task", "block", claimable, "--reason", "Wait for promotion")[
        "data"
    ]["hold_id"]
    blocked_package = command(
        "worker", "next", "--worker-id", worker_id, "--contract", claimable
    )
    if blocked_package is not None or snapshot()["claims"].get(claimable) is not None:
        raise RuntimeError("Worker claim bypassed an active hold")
    command(
        "task",
        "promote",
        claimable,
        "--hold",
        claim_hold,
        "--reason",
        "Allow the fixture Worker to pull the now-ready task.",
        "--idempotency-key",
        "installed-promotion-enables-claim",
    )
    package = command(
        "worker", "next", "--worker-id", worker_id, "--contract", claimable
    )
    if not isinstance(package, dict) or package.get("contract_id") != claimable:
        raise RuntimeError(
            f"promoted task was not claimable by a real signed Worker: {package}"
        )

    target_ids = (atomic, dependent, activation_blocked, claimable)
    histories_before = {
        target: command("task", "promotions", target)["data"] for target in target_ids
    }
    events_before = {
        target: command("task", "events", target)["data"] for target in target_ids
    }
    before_rebuild = snapshot()
    rebuild = _python_json(
        python, _REBUILD_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    if not rebuild.get("same_ids") or not rebuild.get("same_digest"):
        raise RuntimeError(
            f"SQLite reopen/rebuild changed durable promotion facts: {rebuild}"
        )
    command("redis", "rebuild")
    if snapshot() != before_rebuild:
        raise RuntimeError("Redis rebuild changed the SQLite promotion snapshot")
    histories_after = {
        target: command("task", "promotions", target)["data"] for target in target_ids
    }
    events_after = {
        target: command("task", "events", target)["data"] for target in target_ids
    }
    if histories_after != histories_before or events_after != events_before:
        raise RuntimeError(
            "promotion history/events changed after reopen and Redis rebuild"
        )
    for target, history in histories_after.items():
        event_ids = {item.get("record_id") for item in events_after[target]}
        if any(item.get("record_id") not in event_ids for item in history["history"]):
            raise RuntimeError(
                f"task event feed omitted promotion history for {target}"
            )

    return {
        "classification": "installed_signed_task_promotion_fixture",
        "atomic_multi_hold_release": True,
        "exact_replay_and_later_hold_preserved": True,
        "partial_multi_target_results": True,
        "dependency_and_activation_blockers_preserved": True,
        "held_task_claim_denied_then_signed_claim_granted": True,
        "sqlite_reopen_and_redis_rebuild_equal": True,
        "promotion_history_and_events_equal": True,
        "provider_calls": 0,
        "business_outputs": 0,
    }
