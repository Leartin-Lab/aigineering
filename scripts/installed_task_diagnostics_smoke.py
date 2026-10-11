"""Exercise packaged task attempt history with explicit local Worker fixtures.

Raw command streams and database diagnostics stay in a private evidence folder.
The returned value contains only safe identifiers and verification summaries.
This module has no source-tree imports, so the release harness can extract it
from an sdist and call it with an installed ``aig`` and Python interpreter.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid


_RAW_MARKER = "AIG_PRIVATE_RAW_DIAGNOSTICS_MARKER_20261007"
_ERROR_MARKER = "AIG_PRIVATE_ERROR_DIAGNOSTICS_MARKER_20261007"
_CODE_MARKER = "AIG_PRIVATE_CATEGORY_DIAGNOSTICS_MARKER_20261007"

_DATABASE_FINGERPRINT = r"""
import hashlib, json, sqlite3, sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
try:
    conn.execute("BEGIN")
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    tables = {}
    for name in names:
        quoted = name.replace('"', '""')
        rows = conn.execute(f'SELECT * FROM "{quoted}"').fetchall()
        normalized = [
            [value.hex() if isinstance(value, bytes) else value for value in row]
            for row in rows
        ]
        normalized.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), default=str))
        tables[name] = normalized
    revision = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM runtime_records").fetchone()[0]
    encoded = json.dumps(tables, sort_keys=True, separators=(",", ":"), default=str).encode()
    print(json.dumps({
        "revision": int(revision),
        "table_digest": hashlib.sha256(encoded).hexdigest(),
        "row_counts": {name: len(rows) for name, rows in tables.items()},
    }, sort_keys=True))
finally:
    conn.close()
"""

_REBUILD_AND_REOPEN = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore

path = sys.argv[1]
def record_ids(store):
    return sorted(
        (record.id, record.recorded_at)
        for _revision, record in store.scan_runtime_records()
    )

store = SQLiteStore(path)
before_ids = record_ids(store)
first_digest = store.rebuild_runtime_materializations()
after_ids = record_ids(store)
store.close()
store = SQLiteStore(path)
reopened_ids = record_ids(store)
second_digest = store.rebuild_runtime_materializations()
final_ids = record_ids(store)
store.close()
print(json.dumps({
    "same_ids_after_first_rebuild": before_ids == after_ids,
    "same_ids_after_reopen": after_ids == reopened_ids,
    "same_ids_after_second_rebuild": reopened_ids == final_ids,
    "same_timestamps_after_rebuild": before_ids == after_ids == reopened_ids == final_ids,
    "stable_rebuild_digest": first_digest == second_digest,
    "record_count": len(before_ids),
}, sort_keys=True))
"""

_PREPARE_CONFIG = r"""
import json, sys
from aigineering.run_config import prepare_configured_run

prepared = prepare_configured_run(sys.argv[1])
contract = prepared.contract
if contract is None:
    raise RuntimeError("preflight did not prepare a new task")
print(json.dumps({
    "name": contract.name,
    "inputs": list(contract.inputs),
    "outputs": list(contract.outputs),
    "worker_count": len(prepared.workers),
    "db_path": prepared.db_path,
}, sort_keys=True))
"""

_FIXTURE_WORKER = """import json
from aigineering.agent.worker import WorkerExecutionError
from aigineering.protocol.types import Candidate

RAW_MARKER = "AIG_PRIVATE_RAW_DIAGNOSTICS_MARKER_20261007"
ERROR_MARKER = "AIG_PRIVATE_ERROR_DIAGNOSTICS_MARKER_20261007"
CODE_MARKER = "AIG_PRIVATE_CATEGORY_DIAGNOSTICS_MARKER_20261007"

class Worker:
    def __init__(self, worker_id):
        self.worker_id = worker_id

    def invoke(self, contract, disclosed_assets):
        if contract.name == "installed-diagnostics-failure":
            raise WorkerExecutionError(CODE_MARKER, ERROR_MARKER)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec " + json.dumps({
                "outputs": {name: RAW_MARKER for name in contract.outputs}
            }),
        )

def build_worker(*, worker_id):
    return Worker(worker_id)
"""


def _write_private(path: Path, contents: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(contents)


def _write_stream(logs: Path, number: int, kind: str, value: str) -> str:
    path = logs / f"{number:04d}-{kind}.raw"
    _write_private(path, value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def installed_task_diagnostics_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Run success/failure controls and inspect only fact-derived safe views."""
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("task diagnostics control requires explicit Redis URL")

    workspace = cwd / f"installed-task-diagnostics-{uuid.uuid4().hex}"
    workspace.mkdir(mode=0o700)
    logs = workspace / "private-command-streams"
    logs.mkdir(mode=0o700)
    fixtures = workspace / "fixtures"
    fixtures.mkdir(mode=0o700)
    (workspace / ".aig" / "identity").mkdir(parents=True, mode=0o700)
    _write_private(fixtures / "diagnostics_worker.py", _FIXTURE_WORKER)

    local_env = dict(env)
    local_env["AIGINEERING_REDIS_URL"] = redis_url
    local_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )
    # Expose only this packaged-test Worker module, never the source checkout.
    local_env["PYTHONPATH"] = str(fixtures)

    invocation_log: list[dict] = []

    def execute(
        phase: str,
        argv: list[str],
        *,
        expected_exit: int = 0,
        parse_json: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], object]:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=local_env,
            text=True,
            capture_output=True,
            check=False,
        )
        number = len(invocation_log) + 1
        out_hash = _write_stream(logs, number, "stdout", completed.stdout)
        err_hash = _write_stream(logs, number, "stderr", completed.stderr)
        invocation_log.append(
            {
                "phase": phase,
                "argv": argv,
                "exit_code": completed.returncode,
                "stdout_bytes": len(completed.stdout.encode("utf-8")),
                "stdout_sha256": out_hash,
                "stderr_bytes": len(completed.stderr.encode("utf-8")),
                "stderr_sha256": err_hash,
            }
        )
        _replace_manifest(logs / "commands.json", invocation_log)
        if completed.returncode != expected_exit:
            raise RuntimeError(
                f"{phase} exited {completed.returncode}, expected {expected_exit}; "
                f"private evidence retained in {logs}"
            )
        if not parse_json:
            return completed, None
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{phase} did not return JSON; private evidence retained in {logs}"
            ) from exc
        return completed, payload

    def cli(phase: str, *args: str, expected_exit: int = 0, json_output: bool = True):
        argv = [str(aig), "-C", str(workspace), *args]
        if json_output:
            argv.append("--json")
        return execute(
            phase,
            argv,
            expected_exit=expected_exit,
            parse_json=json_output,
        )

    def fingerprint(label: str) -> dict:
        _completed, payload = execute(
            label,
            [
                str(python),
                "-c",
                _DATABASE_FINGERPRINT,
                str(workspace / ".aig/store.db"),
            ],
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label} did not return a database fingerprint")
        return payload

    def assert_read_only(phase: str, call):
        before = fingerprint(f"{phase}-sqlite-before")
        completed, payload = call()
        after = fingerprint(f"{phase}-sqlite-after")
        if before != after:
            raise RuntimeError(f"{phase} changed the SQLite snapshot")
        return completed, payload

    def run_local_fixture(name: str, output: str, expected_exit: int) -> dict:
        workers = fixtures / f"{name}-workers.toml"
        preset = fixtures / f"{name}.toml"
        input_path = fixtures / f"{name}-input.txt"
        _write_private(
            input_path,
            "Public local test input for the installed task diagnostics fixture.\n",
        )
        _write_private(
            workers,
            '[fleet]\ndb_path = ".aig/store.db"\npoll_interval = 0.01\n'
            '[[workers]]\nid = "installed-task-diagnostics-worker"\nkind = "local"\n'
            'worker_factory = "diagnostics_worker:build_worker"\n',
        )
        _write_private(
            preset,
            f'[run]\nfleet = "{workers.name}"\n'
            '[[inputs]]\nname = "diagnostics_source"\n'
            f'path = "{input_path.name}"\n'
            f'[task]\nname = "{name}"\n'
            f'description = "Deterministic local Worker diagnostics fixture for {name}."\n'
            'inputs = ["diagnostics_source"]\n'
            'activation = "diagnostics_source"\n'
            f'budget = 3\noutputs = ["{output}"]\n',
        )
        database = workspace / ".aig" / "store.db"
        store_existed_before_preflight = database.exists()
        store_before_preflight = (
            fingerprint(f"prepare-config-{name}-sqlite-before")
            if store_existed_before_preflight
            else None
        )
        preflight, _ = execute(
            f"prepare-config-{name}",
            [str(python), "-c", _PREPARE_CONFIG, str(preset)],
        )
        if (
            not isinstance(preflight, subprocess.CompletedProcess)
            or not preflight.stdout
        ):
            raise RuntimeError(f"{name} configured-run preflight returned no result")
        try:
            preflight_payload = json.loads(preflight.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{name} configured-run preflight was not JSON") from exc
        if (
            preflight_payload.get("name") != name
            or preflight_payload.get("inputs") != ["diagnostics_source"]
            or preflight_payload.get("outputs") != [output]
            or preflight_payload.get("worker_count") != 1
            or preflight_payload.get("db_path") != ".aig/store.db"
            or database.exists() != store_existed_before_preflight
        ):
            raise RuntimeError(f"{name} configured-run preflight changed fixture scope")
        if store_existed_before_preflight:
            store_after_preflight = fingerprint(f"prepare-config-{name}-sqlite-after")
            if store_before_preflight != store_after_preflight:
                raise RuntimeError(
                    f"{name} configured-run preflight changed the existing Store"
                )
        _completed, payload = cli(
            f"run-{name}",
            "run",
            "--config",
            str(preset),
            expected_exit=expected_exit,
        )
        if not isinstance(payload, dict) or not payload.get("contract_id"):
            raise RuntimeError(f"{name} fixture did not publish its task")
        if payload.get("contract_id") != payload.get("contract_id", "").strip():
            raise RuntimeError(f"{name} fixture returned a malformed task ID")
        return payload

    def checked_page(phase: str, args: tuple[str, ...]) -> tuple[dict, str]:
        _completed, payload = assert_read_only(
            phase,
            lambda: cli(phase, "task", *args),
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise RuntimeError(f"{phase} returned an unexpected envelope")
        if (
            payload.get("ok") is not True
            or payload["data"].get("integrity", {}).get("consistent") is not True
        ):
            raise RuntimeError(f"{phase} reported an inconsistent selected scope")
        return payload, "passed"

    success = run_local_fixture("installed-diagnostics-success", "success_result", 0)
    failure = run_local_fixture("installed-diagnostics-failure", "failure_result", 1)
    success_id = success["contract_id"]
    failure_id = failure["contract_id"]

    success_runs, _ = checked_page("runs-success-json", ("runs", success_id))
    failure_runs, _ = checked_page("runs-failure-json", ("runs", failure_id))
    success_rows = success_runs["data"].get("runs")
    failure_rows = failure_runs["data"].get("runs")
    if not isinstance(success_rows, list) or len(success_rows) != 1:
        raise RuntimeError("success runs query did not return one exact claim")
    if not isinstance(failure_rows, list) or len(failure_rows) != 1:
        raise RuntimeError("failure runs query did not return one exact claim")
    success_run, failure_run = success_rows[0], failure_rows[0]
    if (
        success_run.get("status") != "submitted"
        or success_run.get("attempt_outcome") != "output_asserted"
        or success_run.get("grant_provenance") != "signed"
        or success_run.get("worker_id") != "installed-task-diagnostics-worker"
    ):
        raise RuntimeError("successful fixture lacks the expected signed attempt")
    if (
        failure_run.get("status") != "invocation_failed"
        or failure_run.get("grant_provenance") != "signed"
        or failure_run.get("worker_id") != "installed-task-diagnostics-worker"
        or not isinstance(failure_run.get("failure"), dict)
        or failure_run["failure"].get("category") != "unknown"
    ):
        raise RuntimeError("failed fixture lacks the expected safe invocation failure")

    success_diag, _ = checked_page(
        "diagnostics-success-json",
        ("diagnostics", success_id, "--claim", success_run["claim_id"]),
    )
    success_events = success_diag["data"].get("events")
    if not isinstance(success_events, list):
        raise RuntimeError("successful diagnostics page is malformed")
    success_types = {row.get("record_type") for row in success_events}
    if (
        not {
            "candidate.received",
            "candidate.committed",
            "attempt.closed",
            "claim.submitted",
        }
        <= success_types
    ):
        raise RuntimeError(
            "successful diagnostics lack exact receipt/head/closure evidence"
        )

    failure_oracle, _ = checked_page(
        "diagnostics-failure-full-oracle",
        (
            "diagnostics",
            failure_id,
            "--claim",
            failure_run["claim_id"],
            "--limit",
            "100",
        ),
    )
    oracle_data = failure_oracle["data"]
    expected_events = oracle_data.get("events")
    if (
        not isinstance(expected_events, list)
        or oracle_data.get("truncated") is not False
        or oracle_data.get("next_cursor") is not None
    ):
        raise RuntimeError("full-page diagnostics oracle was incomplete")

    # A one-row first page makes the continuation path deterministic. Publish
    # unrelated work before following the cursor to verify the fixed watermark.
    _first_completed, first_page = assert_read_only(
        "diagnostics-failure-page-one",
        lambda: cli(
            "diagnostics-failure-page-one",
            "task",
            "diagnostics",
            failure_id,
            "--claim",
            failure_run["claim_id"],
            "--limit",
            "1",
            "--json",
        ),
    )
    if not isinstance(first_page, dict) or first_page.get("ok") is not True:
        raise RuntimeError("failure diagnostics first page was inconsistent")
    first_data = first_page.get("data", {})
    cursor = first_data.get("next_cursor")
    if not isinstance(cursor, str) or not first_data.get("truncated"):
        raise RuntimeError("failure diagnostics did not provide a continuation cursor")
    watermark = first_data.get("watermark")
    observed_at = first_data.get("observed_at")
    first_events = first_data.get("events", [])
    if oracle_data.get("watermark") != watermark:
        raise RuntimeError("full-page oracle and cursor query used different snapshots")
    if len(first_events) != 1:
        raise RuntimeError("failure diagnostics first page has unexpected size")

    # An unrelated task publication advances SQLite after cursor creation.
    _new_task_process, new_task = cli(
        "cursor-unrelated-write",
        "task",
        "create",
        "--name",
        "installed-diagnostics-cursor-unrelated",
        "--output",
        "cursor_result",
        "--budget",
        "1",
    )
    if not isinstance(new_task, dict) or not new_task.get("contract_id"):
        raise RuntimeError(
            "cursor continuation control failed to create unrelated task"
        )

    _rebuild, rebuilt = execute(
        "runtime-reopen-rebuild",
        [str(python), "-c", _REBUILD_AND_REOPEN, str(workspace / ".aig/store.db")],
    )
    if not isinstance(rebuilt, dict) or not all(
        rebuilt.get(key) is True
        for key in (
            "same_ids_after_first_rebuild",
            "same_ids_after_reopen",
            "same_ids_after_second_rebuild",
            "same_timestamps_after_rebuild",
            "stable_rebuild_digest",
        )
    ):
        raise RuntimeError("reopen/rebuild changed immutable runtime record identities")

    pages = [first_events[0]]
    while cursor:
        _page_completed, page = assert_read_only(
            "diagnostics-failure-cursor-page",
            lambda cursor=cursor: cli(
                "diagnostics-failure-cursor-page",
                "task",
                "diagnostics",
                failure_id,
                "--claim",
                failure_run["claim_id"],
                "--limit",
                "1",
                "--cursor",
                cursor,
                "--json",
            ),
        )
        data = page.get("data", {})
        if data.get("watermark") != watermark or data.get("observed_at") != observed_at:
            raise RuntimeError("diagnostics cursor changed its snapshot boundary")
        page_events = data.get("events")
        if not isinstance(page_events, list) or len(page_events) > 1:
            raise RuntimeError("diagnostics cursor page has unexpected shape")
        pages.extend(page_events)
        cursor = data.get("next_cursor")

    event_ids = [row.get("record_id") for row in pages]
    if not event_ids or len(event_ids) != len(set(event_ids)):
        raise RuntimeError(
            "diagnostics cursor duplicated or omitted its event boundary"
        )
    if pages != expected_events:
        raise RuntimeError(
            "cursor pages differ from the full fixed-scope diagnostics event set"
        )
    failure_types = {row.get("record_type") for row in pages}
    if not {"worker.invocation_failed", "claim.released"} <= failure_types:
        raise RuntimeError("invocation failure lacks its exact failure/release facts")
    if "attempt.closed" in failure_types:
        raise RuntimeError(
            "invocation failure was incorrectly represented as attempt.closed"
        )
    if not set(failure_run.get("fact_ids", ())) <= set(event_ids):
        raise RuntimeError("diagnostics omitted facts named by the selected run")
    ordered = [(row.get("revision"), row.get("record_id")) for row in pages]
    if ordered != sorted(ordered):
        raise RuntimeError("diagnostic events are not chronologically ordered")

    private_text = "\n".join(
        json.dumps(
            [success_diag, *pages], ensure_ascii=False, sort_keys=True, default=str
        ).splitlines()
    )
    if any(
        marker in private_text for marker in (_RAW_MARKER, _ERROR_MARKER, _CODE_MARKER)
    ):
        raise RuntimeError("safe diagnostic output exposed private Worker content")

    # Human summaries must obey the same privacy boundary and remain read-only.
    human_runs, _human_runs_payload = assert_read_only(
        "runs-human",
        lambda: cli("runs-human", "task", "runs", failure_id, json_output=False),
    )
    human_diag, _human_diag_payload = assert_read_only(
        "diagnostics-human",
        lambda: cli(
            "diagnostics-human",
            "task",
            "diagnostics",
            failure_id,
            "--claim",
            failure_run["claim_id"],
            json_output=False,
        ),
    )
    for output in (
        human_runs.stdout,
        human_runs.stderr,
        human_diag.stdout,
        human_diag.stderr,
    ):
        if any(
            marker in output for marker in (_RAW_MARKER, _ERROR_MARKER, _CODE_MARKER)
        ):
            raise RuntimeError("human diagnostic output exposed private Worker content")

    return {
        "phases": {
            "local_signed_success": "passed",
            "local_signed_invocation_failure": "passed",
            "runs_json_read_only": "passed",
            "diagnostics_json_read_only": "passed",
            "cursor_after_unrelated_write": "passed",
            "reopen_rebuild_identity": "passed",
            "privacy_markers": "passed",
            "human_output": "passed",
        },
        "success_contract_id": success_id,
        "failure_contract_id": failure_id,
        "success_claim_id": success_run["claim_id"],
        "failure_claim_id": failure_run["claim_id"],
        "success_event_count": len(success_events),
        "failure_event_count": len(pages),
        "failure_status": failure_run["status"],
        "failure_category": failure_run["failure"]["category"],
        "final_runtime_revision": fingerprint("final-sqlite-fingerprint")["revision"],
        "command_count": len(invocation_log),
    }


def _replace_manifest(path: Path, rows: list[dict]) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(rows, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
