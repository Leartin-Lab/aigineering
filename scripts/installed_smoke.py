"""Verify the installed wheel through independent CLI processes."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import traceback
import uuid
import venv


def _write_private_json(directory: Path, prefix: str, payload: dict) -> Path:
    path = directory / f"{prefix}-{uuid.uuid4().hex}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return path


@contextmanager
def smoke_workspace(evidence_dir: Path | None):
    """Keep failed installed runs available for phase-specific diagnosis."""
    workdir = Path(tempfile.mkdtemp(prefix="aigineering-installed-smoke-"))
    try:
        yield workdir
    except Exception as exc:
        summary_path = _write_private_json(
            workdir,
            "failed-installed-run",
            {
                "workspace": str(workdir),
                "evidence_dir": str(evidence_dir) if evidence_dir is not None else None,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise RuntimeError(
            "installed smoke failed "
            f"(error_type={type(exc).__name__}); workspace preserved at {workdir}; "
            f"private summary: {summary_path}"
        ) from None
    else:
        shutil.rmtree(workdir)


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"installed command failed ({result.returncode}): {command[0]}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def json_command(aig: Path, args: list[str], *, cwd: Path, env: dict[str, str]) -> dict:
    output = run([str(aig), *args, "--json"], cwd=cwd, env=env)
    return json.loads(output)


def diagnostic(
    python: Path, database: Path, evidence_dir: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    command = [
        str(python),
        "-m",
        "aigineering.diagnostics",
        str(database),
        "--output-dir",
        str(evidence_dir),
    ]
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        try:
            diagnostic_report = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            diagnostic_report = {}
        error_type = diagnostic_report.get("error_type")
        if not isinstance(error_type, str) or not error_type:
            error_type = "UnknownError"

        failure = {
            "command": command,
            "cwd": str(cwd),
            "database": str(database),
            "evidence_dir": str(evidence_dir),
            "returncode": result.returncode,
            "error_type": error_type,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        failure_path = _write_private_json(
            Path(cwd), "installed-diagnostic-failure", failure
        )
        raise RuntimeError(
            "reconstruction diagnostic failed "
            f"(exit={result.returncode}, error_type={error_type}); "
            f"private details saved to {failure_path}"
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("reconstruction diagnostic returned invalid JSON") from error
    if report.get("status") != "passed":
        raise RuntimeError(
            f"reconstruction diagnostic did not pass: {report.get('status')}"
        )
    return report


def configured_run_smoke(
    aig: Path, python: Path, evidence_dir: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise explicit preset setup and restart with an installed fixture Worker."""
    workspace = cwd / "configured-workspace"
    workspace.mkdir()
    config_dir = cwd / "configured-fixtures"
    config_dir.mkdir()
    (config_dir / "fixture_worker.py").write_text(
        "import json\n"
        "from aigineering.protocol.types import Candidate\n"
        "class Worker:\n"
        "    def __init__(self, worker_id): self.worker_id = worker_id\n"
        "    def invoke(self, contract, disclosed_assets):\n"
        "        assert any(a.name == 'source' and a.content == 'exact source' "
        "for a in disclosed_assets)\n"
        "        return Candidate(worker_id=self.worker_id, raw_output='/exec ' + "
        "json.dumps({'outputs': {name: 'checked' for name in contract.outputs}}))\n"
        "def build_worker(*, worker_id): return Worker(worker_id)\n",
        encoding="utf-8",
    )
    (config_dir / "input.txt").write_text("exact source", encoding="utf-8")
    workers = config_dir / "workers.toml"
    workers.write_text(
        '[fleet]\ndb_path = ".aig/store.db"\npoll_interval = 0.01\n'
        '[[workers]]\nid = "installed-fixture"\nkind = "local"\n'
        'worker_factory = "fixture_worker:build_worker"\n',
        encoding="utf-8",
    )
    preset = config_dir / "run.toml"
    preset.write_text(
        '[run]\nfleet = "workers.toml"\n'
        '[[inputs]]\nname = "source"\npath = "input.txt"\n'
        '[task]\nname = "installed-configured"\ninputs = ["source"]\n'
        'activation = "source"\noutputs = ["configured-report"]\n',
        encoding="utf-8",
    )
    fixture_env = dict(env)
    fixture_env["PYTHONPATH"] = str(config_dir)
    fixture_env["AIG_ACTOR_KEY_FILE"] = str(workspace / ".aig/identity/root.ed25519")
    args = ["-C", str(workspace), "run", "--config", str(preset)]
    first = json_command(aig, args, cwd=cwd, env=fixture_env)
    if first.get("completed") is not True:
        raise RuntimeError(f"configured fixture failed: {first}")
    target = first["contract_id"]
    for command in (
        ["-C", str(workspace), "run", "--config", str(preset), "--resume", target],
        ["-C", str(workspace), "run", "--config", str(workers), "--resume", target],
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
    ):
        resumed = json_command(aig, command, cwd=cwd, env=fixture_env)
        if resumed.get("completed") is not True or resumed.get("contract_id") != target:
            raise RuntimeError("configured resume changed the committed result")
    duplicate = subprocess.run(
        [str(aig), *args, "--json"],
        cwd=cwd,
        env=fixture_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if duplicate.returncode == 0 or "resume" not in duplicate.stderr:
        raise RuntimeError("configured new start accepted historical output")
    (config_dir / "input.txt").unlink()
    resumed_without_source = json_command(
        aig,
        ["-C", str(workspace), "run", "--config", str(preset), "--resume", target],
        cwd=cwd,
        env=fixture_env,
    )
    if resumed_without_source.get("completed") is not True:
        raise RuntimeError("configured resume attempted to reload setup files")
    audit = json_command(
        aig, ["-C", str(workspace), "task", "audit", target], cwd=cwd, env=fixture_env
    )
    if audit["task"]["status"] != "completed":
        raise RuntimeError("configured task audit disagrees with result")
    rebuilt = diagnostic(
        python,
        workspace / ".aig/store.db",
        evidence_dir / "configured-rebuild",
        cwd=cwd,
        env=fixture_env,
    )
    if rebuilt.get("status") != "passed":
        raise RuntimeError("configured runtime reconstruction failed")
    first["reconstruction"] = "passed"
    return first


def artifact_smoke(aig: Path, *, cwd: Path, env: dict[str, str]) -> str:
    """Exercise the packaged artifact path in separate installed CLI processes."""

    def artifact(*args: str) -> dict:
        return json.loads(run([str(aig), "artifact", *args], cwd=cwd, env=env))

    text = "A document can support an exact quotation.\n"
    (cwd / "document.txt").write_text(text, encoding="utf-8")
    source = artifact(
        "import", "document.txt", "--name", "attachment", "--media-type", "text/plain"
    )
    document = artifact("parse", source["id"], "--name", "document")
    evidence = artifact(
        "cite",
        document["id"],
        "--name",
        "evidence",
        "--page",
        "1",
        "--start",
        "0",
        "--end",
        "10",
    )
    (cwd / "cited.md").write_text("An exact quotation.[^c1]", encoding="utf-8")
    (cwd / "bindings.json").write_text(
        json.dumps({"c1": evidence["id"]}), encoding="utf-8"
    )
    report = artifact(
        "report", "cited.md", "--bindings", "bindings.json", "--name", "cited-report"
    )
    ancestry = artifact("lineage", report["id"])
    if len(ancestry["nodes"]) != 4 or len(ancestry["publications"]) != 4:
        raise RuntimeError("artifact ancestry is incomplete")
    artifact("export", report["id"], "--output", "artifact-export", "--include-sources")
    source_files = list((cwd / "artifact-export" / "sources").iterdir())
    if len(source_files) != 1 or source_files[0].read_bytes() != text.encode("utf-8"):
        raise RuntimeError("artifact export did not preserve original bytes")
    return report["id"]


def method_governance_smoke(
    python: Path,
    source_root: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict:
    """Run the checked-in method example through the installed wheel."""
    target = cwd / "method-governance-run"
    script = source_root / "examples" / "method-governance" / "demo.py"
    result = subprocess.run(
        [
            str(python),
            str(script),
            "--worker",
            "fixture",
            "--directory",
            str(target),
        ],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"method governance example failed: {result.stderr}")
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("method governance example returned invalid JSON") from error
    if output.get("status") != "complete" or output.get("rebuild_match") is not True:
        raise RuntimeError(f"method governance example did not complete: {output}")
    return output


def quick_goal_smoke(
    aig: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict:
    """Check installed quick-goal input, session JSON, export, and no-overwrite."""
    goal = "Summarize the supplied field notes as Markdown."
    input_path = cwd / "quick-goal-input.txt"
    output_path = cwd / "quick-goal-output.md"
    input_bytes = "研究记录：温度为 21°C。\nSecond line: naïve café.\n".encode("utf-8")
    input_path.write_bytes(input_bytes)

    result = json.loads(
        run(
            [
                str(aig),
                "run",
                goal,
                "--worker",
                "mock",
                "--input",
                f"field_notes={input_path.name}",
                "--output",
                str(output_path),
                "--json",
            ],
            cwd=cwd,
            env=env,
        )
    )
    outputs = result.get("outputs")
    if (
        result.get("ok") is not True
        or result.get("status") != "complete"
        or not isinstance(result.get("session_id"), str)
        or not result["session_id"]
        or not isinstance(outputs, list)
        or len(outputs) != 1
        or outputs[0].get("exportable") is not True
        or not output_path.is_file()
    ):
        raise RuntimeError("installed quick-goal JSON result/session was incomplete")
    markdown = outputs[0].get("content")
    expected_markdown = (
        markdown
        if isinstance(markdown, str) and markdown.endswith("\n")
        else f"{markdown}\n"
    )
    if not isinstance(
        markdown, str
    ) or output_path.read_bytes() != expected_markdown.encode("utf-8"):
        raise RuntimeError("quick-goal Markdown export did not match its JSON output")

    before = output_path.read_bytes()
    collision = subprocess.run(
        [
            str(aig),
            "run",
            goal,
            "--worker",
            "mock",
            "--input",
            f"field_notes={input_path.name}",
            "--output",
            str(output_path),
            "--json",
        ],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if collision.returncode == 0 or output_path.read_bytes() != before:
        raise RuntimeError("existing quick-goal Markdown output was overwritten")

    evidence = {
        "evidence_kind": "fixture",
        "worker": "mock",
        "input_encoding": "utf-8",
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "session_id": result["session_id"],
        "contract_id": result.get("contract_id"),
        "output_slots": [item.get("slot") for item in outputs],
        "markdown_exported": True,
        "existing_output_preserved": True,
        "provider_calls": 0,
    }
    return evidence


def task_hold_smoke(
    aig: Path, python: Path, contract_id: str, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise human holds before the installed fixture task is executed."""
    placement_args = [
        "task",
        "block",
        contract_id,
        "--reason",
        "Installed hold reason A.",
        "--idempotency-key",
        "installed-hold-a",
    ]
    first = json_command(aig, placement_args, cwd=cwd, env=env)
    second = json_command(
        aig,
        [
            "task",
            "block",
            contract_id,
            "--reason",
            "Installed hold reason B.",
            "--idempotency-key",
            "installed-hold-b",
        ],
        cwd=cwd,
        env=env,
    )
    hold_ids = [first["data"]["record_id"], second["data"]["record_id"]]
    human_placement = run([str(aig), *placement_args], cwd=cwd, env=env)
    if (
        hold_ids[0] not in human_placement
        or "Installed hold reason A." not in human_placement
    ):
        raise RuntimeError("installed readable placement omitted its hold ID or reason")
    human_history = run([str(aig), "task", "holds", contract_id], cwd=cwd, env=env)
    if "ACTIVE" not in human_history or not all(
        item in human_history for item in hold_ids
    ):
        raise RuntimeError("installed readable history omitted active holds")
    replay = json_command(aig, placement_args, cwd=cwd, env=env)
    if replay.get("data") != first["data"]:
        raise RuntimeError("hold replay did not return the original durable placement")
    blocked = json_command(aig, ["task", "status", contract_id], cwd=cwd, env=env)
    if blocked.get("status") != "blocked" or not all(
        f"claim_hold:{hold_id}" in blocked.get("blockers", []) for hold_id in hold_ids
    ):
        raise RuntimeError("installed task status omitted active claim holds")
    if json_command(aig, ["worker", "next"], cwd=cwd, env=env) is not None:
        raise RuntimeError("installed Worker pull claimed a held task")
    direct = run(
        [
            str(python),
            "-c",
            "from aigineering.core.sqlite_store import SQLiteStore; "
            f"s=SQLiteStore({str(cwd / '.aig' / 'store.db')!r}); "
            f"assert s.claim_contract({contract_id!r}, 'held-fixture') is None; "
            "s.close(); print('held')",
        ],
        cwd=cwd,
        env=env,
    )
    if direct != "held":
        raise RuntimeError("installed SQLite claim bypassed a hold")
    release_args = [
        "task",
        "unblock",
        contract_id,
        "--hold",
        hold_ids[0],
        "--reason",
        "Installed release A.",
        "--idempotency-key",
        "release-a",
    ]
    release = json_command(aig, release_args, cwd=cwd, env=env)
    still_blocked = json_command(aig, ["task", "status", contract_id], cwd=cwd, env=env)
    if still_blocked.get("status") != "blocked":
        raise RuntimeError("releasing one hold cleared another hold")
    json_command(
        aig,
        [
            "task",
            "unblock",
            contract_id,
            "--hold",
            hold_ids[1],
            "--reason",
            "Installed release B.",
        ],
        cwd=cwd,
        env=env,
    )
    released_replay = json_command(aig, release_args, cwd=cwd, env=env)
    human_release = run([str(aig), *release_args], cwd=cwd, env=env)
    if (
        release["data"]["record_id"] not in human_release
        or hold_ids[0] not in human_release
    ):
        raise RuntimeError("installed readable release omitted its exact records")
    human_released = run([str(aig), "task", "holds", contract_id], cwd=cwd, env=env)
    if "ACTIVE" in human_released or human_released.count("RELEASED") != 2:
        raise RuntimeError("installed readable history did not retain released holds")
    if released_replay.get("data") != release.get("data"):
        raise RuntimeError("released hold replay did not return its original record")
    ready = json_command(aig, ["task", "status", contract_id], cwd=cwd, env=env)
    if ready.get("status") != "ready":
        raise RuntimeError("releasing all holds did not restore eligibility")
    history = json_command(aig, ["task", "holds", contract_id], cwd=cwd, env=env)
    if len(history.get("data", [])) != 2 or any(
        row.get("active") for row in history["data"]
    ):
        raise RuntimeError("installed hold history lost exact releases")
    json_command(aig, ["redis", "rebuild"], cwd=cwd, env=env)
    rebuilt = json_command(aig, ["task", "holds", contract_id], cwd=cwd, env=env)
    if rebuilt.get("data") != history.get("data"):
        raise RuntimeError("Redis rebuild changed claim hold history")
    events = json_command(aig, ["task", "events", contract_id], cwd=cwd, env=env)
    if not all(
        any(item.get("record_id") == hold_id for item in events.get("data", []))
        for hold_id in hold_ids
    ) or "Installed hold reason" in json.dumps(events):
        raise RuntimeError("hold event feed omitted IDs or copied reason content")
    return {
        "evidence_kind": "fixture",
        "hold_ids": hold_ids,
        "worker_pull_and_direct_claim_blocked": True,
        "exact_release_and_replay": True,
        "rebuild_history_equal": True,
        "provider_calls": 0,
    }


def task_handoff_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Verify active-claim handoff and its successor through installed processes."""
    created = json_command(
        aig,
        [
            "task",
            "create",
            "--name",
            "installed-handoff",
            "--description",
            "Finish the two declared report outputs.",
            "--input",
            "source",
            "--activation",
            "source",
            "--budget",
            "3",
            "--output",
            "installed-handoff-done",
            "--output",
            "installed-handoff-next",
        ],
        cwd=cwd,
        env=env,
    )
    source_id = created["contract_id"]
    retained = json_command(
        aig,
        [
            "asset",
            "add",
            "--name",
            "installed-handoff-done",
            "--content",
            "retained result",
        ],
        cwd=cwd,
        env=env,
    )
    proposal_file = cwd / "late-handoff-candidate.json"
    preparation = run(
        [
            str(python),
            "-c",
            "import json,sys; from pathlib import Path; "
            "from aigineering.core.sqlite_store import SQLiteStore; "
            "from aigineering.local_identity import ensure_local_worker_host; "
            "from aigineering.agent.mock import MockWorker; "
            "from aigineering.runtime import claim_next_package; "
            "from aigineering.runtime_services import sync_runtime; "
            "from aigineering.protocol.envelope import CandidateEnvelope; "
            "from aigineering.protocol.candidate import candidate_proposal_to_dict; "
            "s=SQLiteStore(sys.argv[1]); "
            "host=ensure_local_worker_host(s,MockWorker(worker_id='installed-handoff-old')); "
            "claimed=claim_next_package(s,worker_id=host.worker_id,contract_id=sys.argv[2]); "
            "assert claimed is not None; p=claimed.package; "
            "raw='/exec '+json.dumps({'outputs':{n:'too late' for n in claimed.contract.outputs}}); "
            "e=CandidateEnvelope(contract_id=claimed.contract.id,worker_id=host.worker_id,"
            "raw_output=raw,package_id=p.package_id,claim_id=p.claim_id,claim_epoch=p.claim_epoch,"
            "idempotency_key='late-installed-handoff'); "
            "candidate=host.sign_envelope(e,contract=claimed.contract,"
            "disclosed_assets=claimed.disclosed_assets,allowance=3); "
            "Path(sys.argv[3]).write_text(json.dumps(candidate_proposal_to_dict(candidate))); "
            "sync_runtime(s); print(json.dumps(s.get_claim(sys.argv[2]))); s.close()",
            str(cwd / ".aig" / "store.db"),
            source_id,
            str(proposal_file),
        ],
        cwd=cwd,
        env=env,
    )
    old_claim = json.loads(preparation)
    args = [
        "task",
        "handoff",
        source_id,
        "--reason",
        "Operator is handing over active work.",
        "--hold-reason",
        "Wait for operator confirmation.",
        "--idempotency-key",
        "installed-handoff",
    ]
    handed = json_command(aig, args, cwd=cwd, env=env)
    data = handed["data"]
    target_id, hold_id = data["successor_id"], data["hold_id"]
    if data["budget"] != 3 or data["completed_outputs"] != ["installed-handoff-done"]:
        raise RuntimeError(
            "installed handoff did not transfer remaining work and allowance"
        )
    if retained["id"] not in data["retained_asset_ids"]:
        raise RuntimeError("handoff lost the exact retained output Asset")
    human = run([str(aig), *args], cwd=cwd, env=env)
    if not all(value in human for value in (source_id, target_id, hold_id)):
        raise RuntimeError("readable handoff omitted its exact continuation records")
    replay = json_command(aig, args, cwd=cwd, env=env)
    if replay["data"] != data:
        raise RuntimeError("nullable-budget replay changed the original handoff")
    history = json_command(aig, ["task", "handoffs", source_id], cwd=cwd, env=env)
    if history.get("data") != [data]:
        raise RuntimeError("handoff history did not preserve its durable link")
    if (
        json_command(aig, ["task", "status", source_id], cwd=cwd, env=env)["status"]
        != "cancelled"
    ):
        raise RuntimeError("handoff did not cancel the old attempt")
    if (
        json_command(aig, ["task", "status", target_id], cwd=cwd, env=env)["status"]
        != "blocked"
    ):
        raise RuntimeError("handoff successor bypassed its requested hold")
    checked = run(
        [
            str(python),
            "-c",
            "import sys; from aigineering.core.sqlite_store import SQLiteStore; "
            "from aigineering.core.runtime_projection import RuntimeProjection; "
            "s=SQLiteStore(sys.argv[1]); old=s.get_claim(sys.argv[2]); "
            "assert old['status']=='released' and old['claim_id']==sys.argv[4]; "
            "assert RuntimeProjection(s,s).contract_view(s.get_contract(sys.argv[2])).terminal=='cancelled'; "
            "assert s.get_contract(sys.argv[3]).outputs==('installed-handoff-next',); "
            "assert s.claim_contract(sys.argv[3],'installed-held-successor') is None; "
            "s.close(); print('fenced')",
            str(cwd / ".aig" / "store.db"),
            source_id,
            target_id,
            old_claim["claim_id"],
        ],
        cwd=cwd,
        env=env,
    )
    if checked != "fenced":
        raise RuntimeError("installed handoff did not fence its source claim")
    late = subprocess.run(
        [str(aig), "worker", "submit", "--json", proposal_file.read_text()],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if late.returncode == 0 or json.loads(late.stdout).get("status") != "error":
        raise RuntimeError("old handoff claim accepted a late signed output")
    json_command(
        aig,
        [
            "task",
            "unblock",
            target_id,
            "--hold",
            hold_id,
            "--reason",
            "Ready to continue.",
        ],
        cwd=cwd,
        env=env,
    )
    finished = json_command(
        aig, ["run", "--task", target_id, "--worker", "mock"], cwd=cwd, env=env
    )
    if finished.get("ok") is not True or finished.get("status") != "completed":
        raise RuntimeError("installed successor did not execute its remaining output")
    json_command(aig, ["redis", "rebuild"], cwd=cwd, env=env)
    if json_command(aig, ["task", "handoffs", source_id], cwd=cwd, env=env)["data"] != [
        data
    ]:
        raise RuntimeError("Redis rebuild changed handoff history")
    predecessor = json_command(aig, ["task", "status", source_id], cwd=cwd, env=env)
    if predecessor["status"] != "cancelled":
        raise RuntimeError(
            "successor completion changed its cancelled source: "
            + json.dumps(predecessor)
        )
    if predecessor.get("recoveries"):
        raise RuntimeError("late old-claim rejection scheduled new recovery work")
    run(
        [
            str(python),
            "-c",
            "from aigineering.core.sqlite_store import SQLiteStore; "
            "s=SQLiteStore('.aig/store.db'); "
            "outcomes=[r for _,r in s.scan_runtime_records() "
            "if r.record_type=='candidate_rejection.recovery_unavailable' "
            f"and r.payload.get('contract_id')=={source_id!r}]; "
            "assert len(outcomes)==1; "
            "assert outcomes[0].payload.get('reason')=='terminal_preserved'; "
            "assert outcomes[0].payload.get('terminal')=='cancelled'; s.close()",
        ],
        cwd=cwd,
        env=env,
    )
    for event_target in (source_id, target_id):
        events = json_command(
            aig,
            ["task", "events", event_target, "--limit", "1000"],
            cwd=cwd,
            env=env,
        )
        link_events = [
            item
            for item in events.get("data", [])
            if item.get("record_id") == data["record_id"]
        ]
        if len(link_events) != 1 or set(link_events[0]["contract_ids"]) != {
            source_id,
            target_id,
        }:
            raise RuntimeError(
                "handoff link is missing from source or successor event feed"
            )
    return {
        "evidence_kind": "fixture",
        "source_id": source_id,
        "successor_id": target_id,
        "link_id": data["record_id"],
        "active_claim_released": True,
        "late_signed_output_rejected": True,
        "partial_output_retained": True,
        "nullable_budget_replay_stable": True,
        "held_successor_executed": True,
        "redis_rebuild_history_equal": True,
        "late_rejection_terminal_preserved": True,
        "handoff_event_scoped_to_both_tasks": True,
        "provider_calls": 0,
    }


def task_cli_smoke(
    aig: Path,
    python: Path,
    contract_id: str,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict:
    """Exercise installed task projections, event feed, result export, and Redis."""

    listed = json_command(aig, ["task", "list"], cwd=cwd, env=env)
    rows = listed.get("data", {}).get("tasks")
    row = next(
        (item for item in rows or [] if item.get("contract_id") == contract_id), None
    )
    if listed.get("ok") is not True or row is None or row.get("status") != "completed":
        raise RuntimeError(f"task list omitted the completed fixture task: {listed}")

    board = json_command(
        aig, ["task", "board", "--root", contract_id], cwd=cwd, env=env
    )
    columns = board.get("data", {}).get("columns", {})
    if board.get("ok") is not True or not any(
        item.get("contract_id") == contract_id
        for items in columns.values()
        for item in items
    ):
        raise RuntimeError(f"task board omitted the completed fixture task: {board}")

    stats = json_command(aig, ["task", "stats"], cwd=cwd, env=env)
    counts = stats.get("data", {}).get("by_status", {})
    if stats.get("ok") is not True or counts.get("completed", 0) < 1:
        raise RuntimeError(f"task stats omitted completed work: {stats}")

    shown = json_command(aig, ["task", "show", contract_id], cwd=cwd, env=env)
    if (
        shown.get("ok") is not True
        or shown.get("data", {}).get("task", {}).get("status") != "completed"
        or shown.get("data", {}).get("contract", {}).get("contract_id") != contract_id
    ):
        raise RuntimeError(f"task show disagreed with task completion: {shown}")

    comment_args = [
        "task",
        "comment",
        contract_id,
        "Installed conversation check.",
        "--idempotency-key",
        "installed-comment-1",
    ]
    comment = json_command(aig, comment_args, cwd=cwd, env=env)
    replay = json_command(aig, comment_args, cwd=cwd, env=env)
    if comment.get("ok") is not True or replay != comment:
        raise RuntimeError("identical comment replay did not return its durable record")
    reply = json_command(
        aig,
        [
            "task",
            "comment",
            contract_id,
            "Installed reply.",
            "--reply-to",
            comment["data"]["record_id"],
        ],
        cwd=cwd,
        env=env,
    )
    comments = json_command(aig, ["task", "comments", contract_id], cwd=cwd, env=env)
    if comments.get("data") != [comment["data"], reply.get("data")]:
        raise RuntimeError("installed comment history did not preserve the reply")
    unchanged = json_command(aig, ["task", "status", contract_id], cwd=cwd, env=env)
    if unchanged.get("status") != "completed":
        raise RuntimeError("comment changed a terminal task's status")

    events = json_command(aig, ["task", "events", contract_id], cwd=cwd, env=env)
    if events.get("ok") is not True or not isinstance(events.get("data"), list):
        raise RuntimeError(f"task events returned an invalid envelope: {events}")
    if not any(contract_id in item.get("contract_ids", []) for item in events["data"]):
        raise RuntimeError("task events did not contain the fixture task's records")
    if not any(
        item.get("record_id") == comment["data"]["record_id"] for item in events["data"]
    ):
        raise RuntimeError("task events did not include the committed comment")
    if "Installed conversation check." in json.dumps(events):
        raise RuntimeError("Redis events copied comment text instead of metadata")

    watch_output = run(
        [str(aig), "task", "watch", contract_id, "--timeout", "0", "--json"],
        cwd=cwd,
        env=env,
    )
    try:
        watch_lines = [json.loads(line) for line in watch_output.splitlines() if line]
    except json.JSONDecodeError as error:
        raise RuntimeError("task watch did not emit JSON Lines") from error
    if (
        not watch_lines
        or watch_lines[-1].get("kind") != "watch_finished"
        or not any(item.get("kind") == "event" for item in watch_lines)
    ):
        raise RuntimeError(
            "zero-timeout task watch omitted events or its terminal line"
        )

    export_path = cwd / "installed-task-result.txt"
    result = json_command(
        aig,
        [
            "task",
            "result",
            contract_id,
            "--output",
            "report",
            "--to",
            str(export_path),
        ],
        cwd=cwd,
        env=env,
    )
    result_data = result.get("data", {})
    output = next(
        (
            item
            for item in result_data.get("outputs", [])
            if item.get("name") == "report"
        ),
        None,
    )
    if result.get("ok") is not True or output is None or not export_path.is_file():
        raise RuntimeError(f"task result did not export the declared output: {result}")
    stored_content = json.loads(
        run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; "
                "s=SQLiteStore(sys.argv[1]); a=s.get_asset(sys.argv[2]); "
                "print(json.dumps(a.content, ensure_ascii=False)); s.close()",
                str(cwd / ".aig" / "store.db"),
                output["asset_id"],
            ],
            cwd=cwd,
            env=env,
        )
    )
    if export_path.read_bytes() != stored_content.encode("utf-8"):
        raise RuntimeError(
            "task result export did not preserve exact committed content"
        )
    before = b"keep existing task output\n"
    export_path.write_bytes(before)
    collision = subprocess.run(
        [
            str(aig),
            "task",
            "result",
            contract_id,
            "--output",
            "report",
            "--to",
            str(export_path),
            "--json",
        ],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if collision.returncode == 0 or export_path.read_bytes() != before:
        raise RuntimeError("task result overwrote an existing output file")
    export_path.unlink()

    redis_status = json_command(aig, ["redis", "status"], cwd=cwd, env=env)
    if (
        redis_status.get("ok") is not True
        or redis_status.get("available") is not True
        or redis_status.get("current") is not True
    ):
        raise RuntimeError(f"installed Redis status was not current: {redis_status}")
    for action in ("sync", "rebuild"):
        repaired = json_command(aig, ["redis", action], cwd=cwd, env=env)
        if repaired.get("ok") is not True or repaired.get("current") is not True:
            raise RuntimeError(
                f"installed Redis {action} did not finish current: {repaired}"
            )

    rebuilt_comments = json_command(
        aig, ["task", "comments", contract_id], cwd=cwd, env=env
    )
    if rebuilt_comments.get("data") != comments["data"]:
        raise RuntimeError("Redis rebuild changed the durable conversation")

    closed_env = dict(env)
    closed_env["AIGINEERING_REDIS_URL"] = "redis://127.0.0.1:1/0"
    unavailable = subprocess.run(
        [str(aig), "redis", "status", "--json"],
        cwd=cwd,
        env=closed_env,
        check=False,
        text=True,
        capture_output=True,
    )
    try:
        unavailable_payload = json.loads(unavailable.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("closed-port Redis status did not return JSON") from error
    if (
        unavailable.returncode == 0
        or unavailable_payload.get("available") is not False
        or unavailable_payload.get("reason") != "redis_unavailable"
    ):
        raise RuntimeError(
            f"closed-port Redis status did not fail as unavailable: {unavailable_payload}"
        )

    offline_audit = json_command(
        aig,
        ["--offline", "task", "audit", contract_id],
        cwd=cwd,
        env=closed_env,
    )
    if offline_audit.get("task", {}).get("status") != "completed":
        raise RuntimeError("offline task audit could not read the completed local task")

    return {
        "evidence_kind": "fixture",
        "contract_id": contract_id,
        "task_views": ["list", "board", "stats", "show", "events", "watch"],
        "signed_comments_reply_replay": "passed",
        "result_exact_content_exported": True,
        "existing_result_preserved": True,
        "redis_status_sync_rebuild": "passed",
        "closed_port_reports_unavailable": True,
        "offline_audit": "passed",
        "provider_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path)
    options = parser.parse_args()

    wheel = options.wheel.resolve()
    source_root = options.source_root.resolve()
    if not wheel.is_file():
        parser.error(f"wheel does not exist: {wheel}")

    with smoke_workspace(options.evidence_dir) as workdir:
        venv_dir = workdir / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        venv_bin = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        executable_suffix = ".exe" if os.name == "nt" else ""
        python = venv_bin / f"python{executable_suffix}"
        aig = venv_bin / f"aig{executable_suffix}"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AIG_", "AIGINEERING_", "PYTHONPATH", "PYTHONHOME"))
        }
        # Preserve only the infrastructure address, never provider credentials.
        if os.environ.get("AIGINEERING_REDIS_URL"):
            env["AIGINEERING_REDIS_URL"] = os.environ["AIGINEERING_REDIS_URL"]

        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            cwd=workdir,
            env=env,
        )
        module_path = run(
            [
                str(python),
                "-c",
                "import aigineering; print(aigineering.__file__); print(aigineering.__version__)",
            ],
            cwd=workdir,
            env=env,
        ).splitlines()
        if len(module_path) != 2 or Path(module_path[0]).resolve().is_relative_to(
            source_root
        ):
            raise RuntimeError(
                f"installed import resolved inside source tree: {module_path}"
            )
        if module_path[1] != options.expected_version:
            raise RuntimeError(
                f"installed version {module_path[1]!r} != {options.expected_version!r}"
            )

        domain = json_command(aig, ["domain", "init"], cwd=workdir, env=env)
        if not domain.get("domain_id"):
            raise RuntimeError("domain init did not return a domain ID")
        source = json_command(
            aig,
            ["asset", "add", "--name", "source", "--content", "smoke evidence"],
            cwd=workdir,
            env=env,
        )
        if not source.get("id"):
            raise RuntimeError("asset add did not return an asset ID")
        task = json_command(
            aig,
            [
                "task",
                "create",
                "--name",
                "installed_smoke",
                "--description",
                "installed wheel smoke",
                "--input",
                "source",
                "--activation",
                "source",
                "--output",
                "report",
            ],
            cwd=workdir,
            env=env,
        )
        contract_id = task.get("contract_id")
        if not contract_id:
            raise RuntimeError("task create did not return a contract ID")
        hold_result = task_hold_smoke(aig, python, contract_id, cwd=workdir, env=env)
        run_result = json_command(
            aig, ["run", "--once", "--worker", "mock"], cwd=workdir, env=env
        )
        if not run_result.get("ok"):
            raise RuntimeError(f"mock run did not complete: {run_result}")
        status = json_command(
            aig, ["task", "status", contract_id], cwd=workdir, env=env
        )
        if status.get("status") != "completed":
            raise RuntimeError(f"task did not complete: {status}")
        audit = json_command(aig, ["task", "audit", contract_id], cwd=workdir, env=env)
        if not isinstance(audit, dict):
            raise RuntimeError("task audit did not return an object")
        audit_task = audit.get("task")
        productivity = audit.get("productivity")
        if (
            not isinstance(audit_task, dict)
            or audit_task.get("contract_id") != contract_id
            or audit_task.get("status") != "completed"
            or not isinstance(productivity, dict)
            or productivity.get("root_contract_id") != contract_id
            or contract_id not in productivity.get("contract_ids", [])
        ):
            raise RuntimeError("task audit did not contain the completed root lineage")
        task_cli_result = task_cli_smoke(aig, python, contract_id, cwd=workdir, env=env)
        handoff_result = task_handoff_smoke(aig, python, cwd=workdir, env=env)
        from installed_review_smoke import task_review_smoke

        review_result = task_review_smoke(aig, python, cwd=workdir, env=env)
        from installed_revision_smoke import task_revision_smoke

        revision_result = task_revision_smoke(aig, python, cwd=workdir, env=env)
        from installed_expanded_handoff_smoke import expanded_handoff_smoke

        expanded_handoff_result = expanded_handoff_smoke(
            aig, python, cwd=workdir, env=env
        )

        from installed_task_dependencies_smoke import task_dependency_smoke

        dependency_result = task_dependency_smoke(aig, python, cwd=workdir, env=env)

        from installed_worker_coordination_smoke import worker_coordination_smoke

        coordination_result = worker_coordination_smoke(
            aig, python, cwd=workdir, env=env
        )

        from installed_assignment_smoke import task_assignment_smoke

        assignment_result = task_assignment_smoke(aig, python, cwd=workdir, env=env)

        from installed_worker_profile_smoke import installed_worker_profile_smoke

        worker_profile_result = installed_worker_profile_smoke(
            aig, python, cwd=workdir, env=env
        )

        from installed_effect_ownership_smoke import installed_effect_ownership_smoke

        effect_ownership_result = installed_effect_ownership_smoke(
            python, cwd=workdir, env=env
        )

        from installed_task_priority_smoke import installed_task_priority_smoke

        priority_result = installed_task_priority_smoke(
            aig, python, cwd=workdir, env=env
        )

        evidence_dir = (
            options.evidence_dir.resolve()
            if options.evidence_dir
            else workdir / "reconstruction-evidence"
        )
        quick_goal_result = quick_goal_smoke(
            aig,
            cwd=workdir,
            env=env,
        )
        method_result = method_governance_smoke(
            python, source_root, cwd=workdir, env=env
        )
        artifact_id = artifact_smoke(aig, cwd=workdir, env=env)
        report = diagnostic(
            python,
            workdir / ".aig" / "store.db",
            evidence_dir,
            cwd=workdir,
            env=env,
        )
        if (
            not (evidence_dir / "manifest.json").is_file()
            or report.get("status") != "passed"
        ):
            raise RuntimeError("reconstruction evidence is missing or incomplete")
        configured_result = configured_run_smoke(
            aig, python, evidence_dir, cwd=workdir, env=env
        )
        with (evidence_dir / "quick-goal.json").open("x", encoding="utf-8") as stream:
            json.dump(quick_goal_result, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        (evidence_dir / "installed-task-cli.json").write_text(
            json.dumps(task_cli_result, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence_dir / "installed-task-holds.json").write_text(
            json.dumps(hold_result, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence_dir / "installed-task-handoff.json").write_text(
            json.dumps(handoff_result, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence_dir / "installed-task-review.json").write_text(
            json.dumps(review_result, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence_dir / "installed-task-revision.json").write_text(
            json.dumps(revision_result, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence_dir / "installed-expanded-handoff.json").write_text(
            json.dumps(expanded_handoff_result, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "installed-task-dependencies.json").write_text(
            json.dumps(dependency_result, sort_keys=True) + "\n", encoding="utf-8"
        )
        with (evidence_dir / "installed-worker-coordination.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(coordination_result, stream, sort_keys=True)
            stream.write("\n")
        with (evidence_dir / "installed-task-assignments.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(assignment_result, stream, sort_keys=True)
            stream.write("\n")
        with (evidence_dir / "installed-worker-profile.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(worker_profile_result, stream, sort_keys=True)
            stream.write("\n")
        with (evidence_dir / "installed-effect-ownership.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(effect_ownership_result, stream, sort_keys=True)

        with (evidence_dir / "installed-task-priority.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(priority_result, stream, sort_keys=True)
            stream.write("\n")
        (evidence_dir / "configured-run.json").write_text(
            json.dumps(configured_result, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence_dir / "method-governance.json").write_text(
            json.dumps(method_result, sort_keys=True) + "\n", encoding="utf-8"
        )

        source_status = json_command(
            aig, ["task", "status", contract_id], cwd=workdir, env=env
        )
        if source_status.get("status") != "completed":
            raise RuntimeError(
                "source task status changed after reconstruction diagnostic"
            )
        reopen_check = run(
            [
                str(python),
                "-c",
                "from aigineering.core.sqlite_store import SQLiteStore; "
                "from aigineering.business.artifacts import lineage; "
                f"s=SQLiteStore({str(workdir / '.aig' / 'store.db')!r}); "
                f"assert s.get_contract({contract_id!r}) is not None; "
                f"assert len(lineage({artifact_id!r}, s.get_asset)['nodes']) == 4; "
                "s.close(); print('reopened')",
            ],
            cwd=workdir,
            env=env,
        )
        if reopen_check != "reopened":
            raise RuntimeError("SQLite reopen check did not complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
