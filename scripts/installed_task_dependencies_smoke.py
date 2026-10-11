"""Installed CLI acceptance for signed task prerequisite dependencies.

This is a fixture-backed runtime check, not provider evidence. The fixture
Worker runs through the ordinary installed fleet/claim/Candidate path.
"""

from __future__ import annotations

from pathlib import Path

from installed_smoke import json_command, run


def task_dependency_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise claim gating, release, event relay and rebuild in a subworkspace."""
    workspace = cwd / "installed-task-dependencies"
    workspace.mkdir()
    fixture_dir = cwd / "dependency-fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "dependency_worker.py").write_text(
        "import json\n"
        "from aigineering.protocol.types import Candidate\n"
        "class Worker:\n"
        "    def __init__(self, worker_id): self.worker_id = worker_id\n"
        "    def invoke(self, contract, disclosed_assets):\n"
        "        return Candidate(worker_id=self.worker_id, raw_output='/exec ' + "
        "json.dumps({'outputs': {name: 'fixture-output:' + name "
        "for name in contract.outputs}}))\n"
        "def build_worker(*, worker_id): return Worker(worker_id)\n",
        encoding="utf-8",
    )
    workers = fixture_dir / "workers.toml"
    workers.write_text(
        '[fleet]\ndb_path = ".aig/store.db"\npoll_interval = 0.01\n'
        '[[workers]]\nid = "dependency-fixture-worker"\nkind = "local"\n'
        'worker_factory = "dependency_worker:build_worker"\n',
        encoding="utf-8",
    )
    fixture_env = dict(env)
    fixture_env["PYTHONPATH"] = str(fixture_dir)
    fixture_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )

    json_command(
        aig, ["-C", str(workspace), "domain", "init"], cwd=cwd, env=fixture_env
    )
    prerequisite = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            "dependency-source",
            "--description",
            "Produce the exact prerequisite output.",
            "--budget",
            "2",
            "--output",
            "source-proof",
        ],
        cwd=cwd,
        env=fixture_env,
    )["contract_id"]
    target = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            "dependency-target",
            "--description",
            "Wait for the exact prerequisite task.",
            "--budget",
            "2",
            "--output",
            "dependent-result",
        ],
        cwd=cwd,
        env=fixture_env,
    )["contract_id"]
    link_args = [
        "-C",
        str(workspace),
        "task",
        "link",
        target,
        "--depends-on",
        prerequisite,
        "--reason",
        "Requires source proof.",
        "--idempotency-key",
        "installed-dep-link",
    ]
    linked = json_command(aig, link_args, cwd=cwd, env=fixture_env)
    link_data = linked["data"]
    replay = json_command(aig, link_args, cwd=cwd, env=fixture_env)
    if replay["data"] != link_data:
        raise RuntimeError("dependency placement replay changed its durable fact")

    blocked = json_command(
        aig, ["-C", str(workspace), "task", "status", target], cwd=cwd, env=fixture_env
    )
    if blocked.get("status") != "blocked" or not any(
        str(item).startswith(
            f"prerequisite:{link_data['constraint_id']}:{prerequisite}:"
        )
        for item in blocked.get("blockers", [])
    ):
        raise RuntimeError(f"missing prerequisite did not block task status: {blocked}")
    # SQLite remains the claim authority: bypassing the app must still observe
    # the exact dependency gate.
    direct_claim = run(
        [
            str(python.resolve()),
            "-c",
            "from aigineering.core.sqlite_store import SQLiteStore; "
            f"s=SQLiteStore({str(workspace / '.aig' / 'store.db')!r}); "
            f"assert s.claim_contract({target!r}, 'installed-dependency-check') is None; "
            "s.close(); print('blocked')",
        ],
        cwd=cwd,
        env=fixture_env,
    )
    if direct_claim != "blocked":
        raise RuntimeError("direct installed SQLite claim bypassed the prerequisite")

    prerequisite_run = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "fleet",
            "run",
            "--config",
            str(workers),
            "--task",
            prerequisite,
        ],
        cwd=cwd,
        env=fixture_env,
    )
    if prerequisite_run.get("completed") is not True:
        raise RuntimeError(
            f"fixture Worker did not complete prerequisite: {prerequisite_run}"
        )
    ready = json_command(
        aig, ["-C", str(workspace), "task", "status", target], cwd=cwd, env=fixture_env
    )
    if ready.get("status") != "ready":
        raise RuntimeError(
            f"typed prerequisite completion did not unblock target: {ready}"
        )
    target_run = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "fleet",
            "run",
            "--config",
            str(workers),
            "--task",
            target,
        ],
        cwd=cwd,
        env=fixture_env,
    )
    if target_run.get("completed") is not True:
        raise RuntimeError(
            f"fixture Worker did not complete dependent task: {target_run}"
        )
    link_replay_after_terminal = json_command(aig, link_args, cwd=cwd, env=fixture_env)
    if link_replay_after_terminal["data"] != link_data:
        raise RuntimeError("terminal-task dependency replay changed its durable fact")

    export_path = cwd / "installed-dependency-result.txt"
    exported = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "result",
            target,
            "--output",
            "dependent-result",
            "--to",
            str(export_path),
        ],
        cwd=cwd,
        env=fixture_env,
    )
    output = next(
        (
            item
            for item in exported.get("data", {}).get("outputs", [])
            if item.get("name") == "dependent-result"
        ),
        None,
    )
    if (
        output is None
        or export_path.read_text(encoding="utf-8") != "fixture-output:dependent-result"
    ):
        raise RuntimeError(f"dependent result export was not exact: {exported}")

    target_events = json_command(
        aig, ["-C", str(workspace), "task", "events", target], cwd=cwd, env=fixture_env
    )["data"]
    prerequisite_events = json_command(
        aig,
        ["-C", str(workspace), "task", "events", prerequisite],
        cwd=cwd,
        env=fixture_env,
    )["data"]
    if not any(
        item.get("record_id") == link_data["record_id"] for item in target_events
    ):
        raise RuntimeError("target event feed omitted its dependency fact")
    if not any(
        item.get("record_id") == link_data["record_id"] for item in prerequisite_events
    ):
        raise RuntimeError("prerequisite event feed omitted the other endpoint")
    links_before = json_command(
        aig,
        ["-C", str(workspace), "task", "dependencies", target],
        cwd=cwd,
        env=fixture_env,
    )["data"]
    json_command(
        aig, ["-C", str(workspace), "redis", "rebuild"], cwd=cwd, env=fixture_env
    )
    links_after = json_command(
        aig,
        ["-C", str(workspace), "task", "dependencies", target],
        cwd=cwd,
        env=fixture_env,
    )["data"]
    if links_after != links_before:
        raise RuntimeError("Redis rebuild changed dependency history")

    held_target = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            "held-relink-target",
            "--description",
            "Relink while held.",
            "--budget",
            "2",
            "--output",
            "held-result",
        ],
        cwd=cwd,
        env=fixture_env,
    )["contract_id"]
    held_prerequisite = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            "held-relink-source",
            "--description",
            "Second exact prerequisite.",
            "--budget",
            "2",
            "--output",
            "held-proof",
        ],
        cwd=cwd,
        env=fixture_env,
    )["contract_id"]
    held_link = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "link",
            held_target,
            "--depends-on",
            held_prerequisite,
            "--reason",
            "Replace while hold protects target.",
        ],
        cwd=cwd,
        env=fixture_env,
    )["data"]
    hold = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "block",
            held_target,
            "--reason",
            "Safe relink window.",
        ],
        cwd=cwd,
        env=fixture_env,
    )["data"]
    released = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "unlink",
            held_target,
            "--link",
            held_link["constraint_id"],
            "--reason",
            "Replace exact prerequisite.",
        ],
        cwd=cwd,
        env=fixture_env,
    )
    still_blocked = json_command(
        aig,
        ["-C", str(workspace), "task", "status", held_target],
        cwd=cwd,
        env=fixture_env,
    )
    if still_blocked.get("status") != "blocked":
        raise RuntimeError(
            "dependency unlink incorrectly released its independent hold"
        )
    json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "unblock",
            held_target,
            "--hold",
            hold["record_id"],
            "--reason",
            "Relink is complete.",
        ],
        cwd=cwd,
        env=fixture_env,
    )
    return {
        "classification": "installed_explicit_fixture",
        "workspace": str(workspace),
        "target_contract_id": target,
        "prerequisite_contract_id": prerequisite,
        "constraint_id": link_data["constraint_id"],
        "released_hold_relink_id": released.get("data", {}).get("record_id"),
        "status": "prerequisite_gate_then_complete",
        "direct_claim_blocked": True,
        "exact_replay_after_terminal": True,
        "events_both_endpoints_and_rebuild_equal": True,
        "result_export_exact": True,
        "provider_calls": 0,
    }
