"""Installed CLI control for task edit successor publication.

The fixture uses signed local Candidates and records explicit hold facts. It
does not call a model provider or claim that an output was produced.
"""

from __future__ import annotations

from pathlib import Path

from installed_assignment_smoke import _REBUILD_SCRIPT, _SNAPSHOT_SCRIPT
from installed_smoke import json_command
from installed_worker_coordination_smoke import _python_json, _task


_CONTRACT_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.wire import contract_to_dict
store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    contract = store.get_contract(sys.argv[2])
    print(json.dumps(contract_to_dict(contract) if contract else None, sort_keys=True))
finally:
    store.close()
"""


def installed_task_edit_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Edit a held task, verify exact replay, and rebuild its durable facts."""
    workspace = cwd / "installed-task-edit"
    workspace.mkdir(mode=0o700)
    database = workspace / ".aig" / "store.db"
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )

    def command(*args: str) -> dict:
        return json_command(aig, ["-C", str(workspace), *args], cwd=cwd, env=local_env)

    def snapshot() -> dict:
        return _python_json(
            python, _SNAPSHOT_SCRIPT, [str(database)], cwd=cwd, env=local_env
        )

    command("domain", "init")
    source = _task(
        aig,
        workspace,
        "installed-edit-source",
        "installed-edit-result",
        cwd=cwd,
        env=local_env,
    )
    source_hold = command(
        "task", "block", source, "--reason", "Wait for the task edit"
    )["data"]["hold_id"]
    worker_id = "installed-task-edit-worker"
    if command("worker", "next", "--worker-id", worker_id, "--contract", source):
        raise RuntimeError("a held source task was claimed before edit")
    if snapshot()["claims"].get(source) is not None:
        raise RuntimeError("held source task acquired a claim")

    edit_args = [
        "task",
        "edit",
        source,
        "--title",
        "installed edited title",
        "--body",
        "",
        "--reason",
        "Replace the stale task instructions",
        "--idempotency-key",
        "installed-task-edit-exact-replay",
    ]
    edited = command(*edit_args)["data"]
    edit = edited.get("edit", {})
    mapping = edit.get("hold_mapping", [])
    if (
        edited.get("record_type") != "task.handed_off"
        or edited.get("contract_id") != source
        or edited.get("source_status") != "cancelled"
        or edited.get("source_hold_ids") != [source_hold]
        or edit.get("changed_fields") != ["title", "body"]
        or len(mapping) != 1
        or mapping[0].get("source_hold_id") != source_hold
        or not mapping[0].get("successor_hold_id")
    ):
        raise RuntimeError(f"task edit omitted exact successor/hold evidence: {edited}")
    successor = edited["successor_id"]
    successor_hold = mapping[0]["successor_hold_id"]
    status = command("task", "status", successor)
    if status.get("status") != "blocked":
        raise RuntimeError(
            f"copied source hold did not keep successor blocked: {status}"
        )
    if command("worker", "next", "--worker-id", worker_id, "--contract", successor):
        raise RuntimeError("successor was claimable while its copied hold was open")

    # A later hold is not part of the edit Candidate and must survive exact
    # replay, while the copied original hold remains tied to its original fact.
    later_hold = command(
        "task", "block", successor, "--reason", "Keep the successor paused"
    )["data"]["hold_id"]
    replay = command(*edit_args)["data"]
    if replay != edited:
        raise RuntimeError("exact task edit replay changed its durable link/result")
    hold_rows = command("task", "holds", successor)["data"]
    active_ids = sorted(row["record_id"] for row in hold_rows if row.get("active"))
    if active_ids != sorted([successor_hold, later_hold]):
        raise RuntimeError(
            f"edit replay changed later/open successor holds: {hold_rows}"
        )
    history = command("task", "edits", source)["data"]
    if len(history) != 1 or history[0].get("record_id") != edited["record_id"]:
        raise RuntimeError("edit replay appended or hid immutable edit history")

    # Releasing the old source hold does not release either successor hold.
    command(
        "task",
        "unblock",
        source,
        "--hold",
        source_hold,
        "--reason",
        "Close the obsolete source hold",
    )
    replay_after_source_release = command(*edit_args)["data"]
    if replay_after_source_release != edited:
        raise RuntimeError("historical edit replay depended on an open source hold")
    source_status = command("task", "status", source)
    if source_status.get("status") != "cancelled":
        raise RuntimeError(
            f"source terminal status changed after edit: {source_status}"
        )

    events = command("task", "events", source, "--after", "0", "--limit", "10000")[
        "data"
    ]
    if not any(
        row.get("record_type") == "task.handed_off"
        and row.get("record_id") == edited["record_id"]
        for row in events
    ):
        raise RuntimeError("required Redis task feed omitted the edit link event")

    before = snapshot()
    rebuilt = _python_json(
        python, _REBUILD_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    after = snapshot()
    if not rebuilt.get("same_ids") or not rebuilt.get("same_digest"):
        raise RuntimeError(f"SQLite projection rebuild changed edit facts: {rebuilt}")
    if before["record_ids"] != after["record_ids"]:
        raise RuntimeError("reopening/rebuild changed immutable task edit history")
    successor_contract = _python_json(
        python,
        _CONTRACT_SCRIPT,
        [str(database), successor],
        cwd=cwd,
        env=local_env,
    )
    if (
        successor_contract is None
        or successor_contract.get("name") != "installed edited title"
        or successor_contract.get("description") != ""
    ):
        raise RuntimeError("reopened edited successor did not preserve exact text")
    if command("worker", "next", "--worker-id", worker_id, "--contract", source):
        raise RuntimeError("cancelled source became claimable after successor edit")
    command(
        "task",
        "unblock",
        successor,
        "--hold",
        successor_hold,
        "--reason",
        "Release the copied source hold",
    )
    command(
        "task",
        "unblock",
        successor,
        "--hold",
        later_hold,
        "--reason",
        "Release the later successor hold",
    )
    package = command(
        "worker", "next", "--worker-id", worker_id, "--contract", successor
    )
    if (
        not isinstance(package, dict)
        or package.get("contract_id") != successor
        or not package.get("claim_id")
        or not package.get("claim_epoch")
    ):
        raise RuntimeError(
            f"signed Worker CLI did not claim the unblocked edited successor: {package}"
        )
    claimed = snapshot()["claims"].get(successor)
    if (
        claimed is None
        or claimed.get("claim_id") != package["claim_id"]
        or claimed.get("worker_id") != worker_id
    ):
        raise RuntimeError("signed successor claim is absent from SQLite after reopen")
    return {
        "source_contract_id": source,
        "successor_contract_id": successor,
        "handoff_record_id": edited["record_id"],
        "copied_source_hold_id": source_hold,
        "successor_hold_id": successor_hold,
        "later_hold_id": later_hold,
        "edit_history_count": len(history),
        "redis_event_count_before_successor_claim": len(events),
        "rebuild_same_ids": rebuilt.get("same_ids"),
        "rebuild_same_digest": rebuilt.get("same_digest"),
        "successor_snapshot_available": True,
        "successor_claim_id": package["claim_id"],
        "successor_claim_worker_id": claimed["worker_id"],
        "cancelled_source_claim_absent": True,
    }
