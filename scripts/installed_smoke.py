"""Verify the installed wheel through independent CLI processes."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import traceback
import uuid
import venv


_FAILURE_SCHEMA = "installed-smoke-failure/v1"
_FAILURE_PHASES = frozenset(
    {
        "input_validation",
        "venv_create",
        "wheel_install",
        "installed_import",
        "domain_init",
        "fixture_seed",
        "root_task_smoke",
        "feature_smokes",
        "reconstruction",
        "configured_run",
        "evidence_write",
        "post_rebuild_check",
        "unknown",
    }
)
_FAILURE_CODES = frozenset(
    {
        "missing_input",
        "command_failed",
        "invalid_response",
        "assertion_failed",
        "reconstruction_failed",
        "unexpected",
    }
)
_ACTIVE_PHASE = "input_validation"


class SmokeFailure(RuntimeError):
    """A bounded failure classification for the installed-artifact runner."""

    def __init__(self, message: str, *, error_code: str, returncode: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.returncode = returncode


def _set_phase(phase: str) -> None:
    global _ACTIVE_PHASE
    _ACTIVE_PHASE = phase if phase in _FAILURE_PHASES else "unknown"


def _safe_failure_payload(
    *,
    run_id: str,
    phase: str,
    error_code: str,
    returncode: int | None,
    source_revision: str | None,
    wheel_sha256: str | None,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("run_id must be a generated UUID hex")
    phase = phase if isinstance(phase, str) and phase in _FAILURE_PHASES else "unknown"
    error_code = (
        error_code
        if isinstance(error_code, str) and error_code in _FAILURE_CODES
        else "unexpected"
    )
    if type(returncode) is not int or not -255 <= returncode <= 255:
        returncode = None
    if not isinstance(source_revision, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", source_revision
    ):
        source_revision = None
    if not isinstance(wheel_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", wheel_sha256
    ):
        wheel_sha256 = None
    return {
        "schema": _FAILURE_SCHEMA,
        "run_id": run_id,
        "phase": phase,
        "error_code": error_code,
        "returncode": returncode,
        "source_revision": source_revision,
        "wheel_sha256": wheel_sha256,
    }


def _write_safe_failure(directory: Path, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"installed-smoke-failure-{payload['run_id']}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
    return path


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, SmokeFailure):
        return (
            exc.error_code
            if isinstance(exc.error_code, str) and exc.error_code in _FAILURE_CODES
            else "unexpected"
        )
    if isinstance(exc, AssertionError):
        return "assertion_failed"
    if _ACTIVE_PHASE == "reconstruction":
        return "reconstruction_failed"
    return "unexpected"


def _write_private_json(directory: Path, prefix: str, payload: dict) -> Path:
    path = directory / f"{prefix}-{uuid.uuid4().hex}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return path


def _write_result_evidence(
    *,
    evidence_dir: Path,
    private_results_dir: Path,
    workdir: Path,
    scenario: str,
    payload: dict,
) -> dict:
    """Retain full results privately and emit a small upload-safe summary."""
    private_path = _write_private_json(workdir, f"scenario-{scenario}", payload)
    private_bytes = private_path.read_bytes()
    private_results_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    retained_path = private_results_dir / private_path.name
    os.replace(private_path, retained_path)
    summary = {
        "scenario": scenario,
        "status": "passed",
        "private_result_sha256": hashlib.sha256(private_bytes).hexdigest(),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    summary_path = evidence_dir / f"{scenario}.json"
    descriptor = os.open(summary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(summary, stream, sort_keys=True)
        stream.write("\n")
    return summary


@contextmanager
def smoke_workspace(
    evidence_dir: Path | None,
    *,
    failure_context: dict | None = None,
):
    """Keep failed installed runs available for phase-specific diagnosis."""
    _set_phase("venv_create")
    try:
        workdir = Path(tempfile.mkdtemp(prefix="aigineering-installed-smoke-"))
    except Exception:
        safe_status = "not_configured"
        if evidence_dir is not None:
            safe_status = "failed"
            try:
                payload = _safe_failure_payload(
                    run_id=(failure_context or {}).get("run_id") or uuid.uuid4().hex,
                    phase=_ACTIVE_PHASE,
                    error_code="unexpected",
                    returncode=None,
                    source_revision=(failure_context or {}).get("source_revision"),
                    wheel_sha256=(failure_context or {}).get("wheel_sha256"),
                )
                _write_safe_failure(evidence_dir, payload)
                safe_status = "written"
            except Exception:
                pass
        raise RuntimeError(
            "installed smoke failed "
            f"(phase={_ACTIVE_PHASE}, error_code=unexpected, "
            f"private_capture=unavailable, safe_capture={safe_status})"
        ) from None
    _set_phase("input_validation")
    try:
        yield workdir
    except Exception as exc:
        private_status = "failed"
        try:
            _write_private_json(
                workdir,
                "failed-installed-run",
                {
                    "workspace": str(workdir),
                    "evidence_dir": str(evidence_dir)
                    if evidence_dir is not None
                    else None,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            private_status = "written"
        except Exception:
            pass
        safe_status = "not_configured"
        if evidence_dir is not None:
            safe_status = "failed"
            try:
                payload = _safe_failure_payload(
                    run_id=(failure_context or {}).get("run_id") or uuid.uuid4().hex,
                    phase=_ACTIVE_PHASE,
                    error_code=_failure_code(exc),
                    returncode=(
                        exc.returncode if isinstance(exc, SmokeFailure) else None
                    ),
                    source_revision=(failure_context or {}).get("source_revision"),
                    wheel_sha256=(failure_context or {}).get("wheel_sha256"),
                )
                _write_safe_failure(evidence_dir, payload)
                safe_status = "written"
            except Exception:
                pass
        raise RuntimeError(
            "installed smoke failed "
            f"(phase={_ACTIVE_PHASE}, error_code={_failure_code(exc)}, "
            f"private_capture={private_status}, safe_capture={safe_status})"
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
        raise SmokeFailure(
            f"installed command failed ({result.returncode}): {command[0]}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            error_code="command_failed",
            returncode=result.returncode,
        )
    return result.stdout.strip()


def json_command(aig: Path, args: list[str], *, cwd: Path, env: dict[str, str]) -> dict:
    output = run([str(aig), *args, "--json"], cwd=cwd, env=env)
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise SmokeFailure(
            "installed command returned invalid JSON", error_code="invalid_response"
        ) from error


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
        try:
            _write_private_json(Path(cwd), "installed-diagnostic-failure", failure)
        except Exception:
            pass
        raise SmokeFailure(
            "reconstruction diagnostic failed",
            error_code="reconstruction_failed",
            returncode=result.returncode,
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
    revision_command = [
        str(python),
        "-c",
        "import sys\nfrom aigineering.core.sqlite_store import SQLiteStore\n"
        "with SQLiteStore(sys.argv[1]) as store:\n"
        "    print(store.get_runtime_revision())\n",
        str(workspace / ".aig/store.db"),
    ]
    before_replay = int(run(revision_command, cwd=cwd, env=fixture_env))
    duplicate = json_command(aig, args, cwd=cwd, env=fixture_env)
    if duplicate.get("completed") is not True or duplicate.get("contract_id") != target:
        raise RuntimeError("configured setup replay changed the committed result")
    if int(run(revision_command, cwd=cwd, env=fixture_env)) != before_replay:
        raise RuntimeError("configured setup replay created new facts")
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

    store_path = cwd / ".aig" / "store.db"

    def runtime_ids() -> list[str]:
        output = run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; "
                "s=SQLiteStore(sys.argv[1]); print(json.dumps([r.id for _,r in s.scan_runtime_records()])); s.close()",
                str(store_path),
            ],
            cwd=cwd,
            env=env,
        )
        return json.loads(output)

    before_read_only = runtime_ids()
    comments = json_command(aig, ["task", "comments", contract_id], cwd=cwd, env=env)
    if comments.get("ok") is not True or comments.get("data") != []:
        raise RuntimeError("unlinked task comment history was not empty")
    unchanged = json_command(aig, ["task", "status", contract_id], cwd=cwd, env=env)
    if unchanged.get("status") != "completed":
        raise RuntimeError("read-only task history changed its status")

    events = json_command(aig, ["task", "events", contract_id], cwd=cwd, env=env)
    if events.get("ok") is not True or not isinstance(events.get("data"), list):
        raise RuntimeError(f"task events returned an invalid envelope: {events}")
    if not any(contract_id in item.get("contract_ids", []) for item in events["data"]):
        raise RuntimeError("task events did not contain the fixture task's records")
    if runtime_ids() != before_read_only:
        raise RuntimeError("task comments/events reads changed runtime facts")

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
        raise RuntimeError("Redis rebuild changed the read-only comment history")

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
        "read_only_empty_comments_history": "passed",
        "result_exact_content_exported": True,
        "existing_result_preserved": True,
        "redis_status_sync_rebuild": "passed",
        "closed_port_reports_unavailable": True,
        "offline_audit": "passed",
        "provider_calls": 0,
    }


def bulk_cancel_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Retain installed signed batch cancellation and live-claim fencing coverage."""
    workspace_args = []

    def command(args: list[str]) -> dict:
        return json_command(aig, [*workspace_args, *args], cwd=cwd, env=env)

    created = []
    for suffix in ("open", "claimed"):
        task = command(
            [
                "task",
                "create",
                "--name",
                f"installed-cancel-{suffix}",
                "--output",
                f"cancel_{suffix}",
                "--budget",
                "2",
            ]
        )
        created.append(task["contract_id"])
    open_id, claimed_id = created
    package = command(["worker", "next", "--contract", claimed_id])
    if package.get("contract_id") != claimed_id or not package.get("claim_id"):
        raise RuntimeError("bulk cancellation fixture did not establish a live claim")

    base_key = "installed-smoke-bulk-cancel"
    args = [
        "task",
        "cancel",
        open_id,
        claimed_id,
        "--reason",
        "Installed canonical cancellation fixture.",
        "--idempotency-key",
        base_key,
    ]
    first = command(args)
    rows = first.get("data", {}).get("results")
    if first.get("ok") is not True or not isinstance(rows, list) or len(rows) != 2:
        raise RuntimeError(f"bulk cancellation did not return both targets: {first}")
    if {row.get("contract_id") for row in rows} != set(created) or any(
        row.get("outcome") != "accepted" for row in rows
    ):
        raise RuntimeError("bulk cancellation did not accept every exact target")
    keys = {row["contract_id"]: row.get("idempotency_key") for row in rows}
    if len(set(keys.values())) != 2 or any(not key for key in keys.values()):
        raise RuntimeError("bulk cancellation omitted unique target replay keys")

    store_path = cwd / ".aig" / "store.db"

    def record_ids() -> list[str]:
        output = run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; "
                "s=SQLiteStore(sys.argv[1]); print(json.dumps([r.id for _,r in s.scan_runtime_records()])); s.close()",
                str(store_path),
            ],
            cwd=cwd,
            env=env,
        )
        return json.loads(output)

    before_replay = record_ids()
    replay = command(
        [
            "task",
            "cancel",
            claimed_id,
            open_id,
            "--reason",
            "Installed canonical cancellation fixture.",
            "--idempotency-key",
            base_key,
        ]
    )
    replay_rows = replay.get("data", {}).get("results")
    if not isinstance(replay_rows, list) or record_ids() != before_replay:
        raise RuntimeError("reordered exact cancellation replay appended facts")
    for row in replay_rows:
        original = next(
            item for item in rows if item["contract_id"] == row["contract_id"]
        )
        if any(
            row.get(field) != original.get(field)
            for field in ("idempotency_key", "candidate_id", "fact_ids", "data")
        ):
            raise RuntimeError("reordered cancellation replay changed original facts")
    singleton = command(
        [
            "task",
            "cancel",
            open_id,
            "--reason",
            "Installed canonical cancellation fixture.",
            "--idempotency-key",
            keys[open_id],
        ]
    )
    original_open = next(item for item in rows if item["contract_id"] == open_id)
    if any(
        singleton.get("data", {}).get(field) != original_open.get("data", {}).get(field)
        for field in ("record_id", "candidate_id", "fact_ids", "idempotency_key")
    ):
        raise RuntimeError("single-target returned-key replay changed durable facts")

    changed = subprocess.run(
        [
            str(aig),
            "task",
            "cancel",
            open_id,
            claimed_id,
            "--reason",
            "Changed cancellation intent must not replay.",
            "--idempotency-key",
            base_key,
            "--json",
        ],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    try:
        changed_payload = json.loads(changed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("changed cancellation did not return JSON") from error
    changed_rows = changed_payload.get("data", {}).get("results", [])
    if (
        not isinstance(changed_rows, list)
        or len(changed_rows) != 2
        or any(row.get("outcome") == "accepted" for row in changed_rows)
    ):
        raise RuntimeError("changed cancellation intent was reported as accepted")
    for row in changed_rows:
        original = next(
            item for item in rows if item["contract_id"] == row["contract_id"]
        )
        if row.get("candidate_id") == original.get("candidate_id"):
            raise RuntimeError("changed intent reused the original Candidate identity")
    for contract_id in created:
        status = command(["task", "status", contract_id])
        if status.get("status") != "cancelled":
            raise RuntimeError("signed batch cancellation did not cancel every target")
    no_reclaim = command(["worker", "next", "--contract", claimed_id])
    if isinstance(no_reclaim, dict) and no_reclaim.get("contract_id"):
        raise RuntimeError("bulk-cancelled live claim became claimable again")
    return {
        "cancelled_targets": len(created),
        "live_claim_fenced": True,
        "reordered_exact_replay_added_no_facts": True,
        "single_target_returned_key_replay": True,
        "changed_payload_did_not_replace_terminal_facts": True,
        "provider_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path)
    options = parser.parse_args()

    wheel = options.wheel
    source_root = options.source_root
    run_id = uuid.uuid4().hex
    revision = os.environ.get("GITHUB_SHA", "")
    source_revision = revision if re.fullmatch(r"[0-9a-fA-F]{40}", revision) else None
    failure_context = {
        "run_id": run_id,
        "source_revision": source_revision,
        "wheel_sha256": None,
    }
    with smoke_workspace(
        options.evidence_dir, failure_context=failure_context
    ) as workdir:
        _set_phase("input_validation")
        try:
            wheel = wheel.resolve()
            source_root = source_root.resolve()
        except (OSError, RuntimeError) as error:
            raise SmokeFailure(
                "wheel or source root could not be resolved",
                error_code="missing_input",
            ) from error
        if not wheel.is_file():
            raise SmokeFailure("required wheel is missing", error_code="missing_input")
        digest = hashlib.sha256()
        try:
            with wheel.open("rb") as wheel_stream:
                for chunk in iter(lambda: wheel_stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise SmokeFailure(
                "required wheel could not be read", error_code="missing_input"
            ) from error
        failure_context["wheel_sha256"] = digest.hexdigest()
        venv_dir = workdir / "venv"
        _set_phase("venv_create")
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

        _set_phase("wheel_install")
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
        _set_phase("installed_import")
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

        _set_phase("domain_init")
        domain = json_command(aig, ["domain", "init"], cwd=workdir, env=env)
        if not domain.get("domain_id"):
            raise RuntimeError("domain init did not return a domain ID")
        _set_phase("fixture_seed")
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
        _set_phase("root_task_smoke")
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
        _set_phase("feature_smokes")
        task_cli_result = task_cli_smoke(aig, python, contract_id, cwd=workdir, env=env)
        bulk_cancel_result = bulk_cancel_smoke(aig, python, cwd=workdir, env=env)
        from installed_review_smoke import task_review_smoke

        review_result = task_review_smoke(aig, python, cwd=workdir, env=env)
        from installed_worker_coordination_smoke import worker_coordination_smoke

        coordination_result = worker_coordination_smoke(
            aig, python, cwd=workdir, env=env
        )
        from installed_ordinary_tool_smoke import ordinary_tool_smoke
        from installed_ordinary_tool_smoke import planning_declaration_smoke
        from installed_ordinary_tool_smoke import planning_completion_smoke

        planning_result = planning_declaration_smoke(aig, python, cwd=workdir, env=env)
        planning_completion_result = planning_completion_smoke(
            aig, python, source_root, cwd=workdir, env=env
        )
        ordinary_tool_result = ordinary_tool_smoke(aig, python, cwd=workdir, env=env)

        from installed_boards_smoke import installed_boards_smoke
        from installed_registration_digest_smoke import (
            installed_registration_digest_smoke,
        )
        from installed_task_context_smoke import installed_task_context_smoke
        from installed_task_diagnostics_smoke import installed_task_diagnostics_smoke
        from installed_task_logs_smoke import installed_task_logs_smoke
        from installed_task_attachment_smoke import installed_task_attachment_smoke

        current_helper_results = {
            "boards": installed_boards_smoke(aig, python, cwd=workdir, env=env),
            "registration_digest": installed_registration_digest_smoke(
                aig, python, cwd=workdir, env=env
            ),
            "task_context": installed_task_context_smoke(
                aig, python, cwd=workdir, env=env
            ),
            "task_diagnostics": installed_task_diagnostics_smoke(
                aig, python, cwd=workdir, env=env
            ),
            "task_logs": installed_task_logs_smoke(aig, python, cwd=workdir, env=env),
            "task_attachments": installed_task_attachment_smoke(
                aig, python, cwd=workdir, env=env
            ),
        }

        evidence_dir = (
            options.evidence_dir.resolve()
            if options.evidence_dir
            else workdir.parent / f"{workdir.name}-evidence"
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
        _set_phase("reconstruction")
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
        private_results_dir = workdir.parent / f"{workdir.name}-private-results"
        _set_phase("configured_run")
        configured_result = configured_run_smoke(
            aig, python, evidence_dir, cwd=workdir, env=env
        )
        _set_phase("evidence_write")
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="quick-goal",
            payload=quick_goal_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="installed-task-cli",
            payload=task_cli_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="installed-task-bulk-cancel",
            payload=bulk_cancel_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="installed-task-review",
            payload=review_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="installed-worker-coordination",
            payload=coordination_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="installed-ordinary-tool",
            payload=ordinary_tool_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="installed-plan-stages",
            payload=planning_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="installed-plan-completion",
            payload=planning_completion_result,
        )
        for name, result in current_helper_results.items():
            _write_result_evidence(
                evidence_dir=evidence_dir,
                private_results_dir=private_results_dir,
                workdir=workdir,
                scenario=f"installed-{name}",
                payload=result,
            )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="configured-run",
            payload=configured_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="method-governance",
            payload=method_result,
        )
        _write_result_evidence(
            evidence_dir=evidence_dir,
            private_results_dir=private_results_dir,
            workdir=workdir,
            scenario="artifact-lineage",
            payload={"artifact_id": artifact_id, "lineage_nodes": 4},
        )

        _set_phase("post_rebuild_check")
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
