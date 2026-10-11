"""Installed-artifact smoke for signed Worker invocation log capture.

The fixture uses the packaged runtime, a normal signed local WorkerHost and an
in-process fake OpenAI-compatible transport. It never contacts a provider and
never treats hand-written JSONL as successful capture evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import socket
import time
import uuid


_MARKERS = (
    "AIG_PRIVATE_LOG_PROMPT_20261007",
    "AIG_PRIVATE_LOG_KEY_20261007",
    "AIG_PRIVATE_LOG_PROVIDER_20261007",
    "AIG_PRIVATE_LOG_OUTPUT_20261007",
    "AIG_PRIVATE_LOG_ALPHA_PROMPT_20261007",
    "AIG_PRIVATE_LOG_BETA_PROMPT_20261007",
    "AIG_PRIVATE_LOG_ALPHA_OUTPUT_20261007",
    "AIG_PRIVATE_LOG_BETA_OUTPUT_20261007",
)

_FIXTURE = r"""
import os, time
from pathlib import Path
from threading import local
from aigineering.agent.llm import LLMWorker, ProviderError

_state = local()

def _transport(url, headers, payload):
    if not url.startswith("http://fixture.invalid/"):
        raise AssertionError("unexpected transport URL")
    prompt = payload["messages"][1]["content"]
    flavor = "alpha" if "AIG_PRIVATE_LOG_ALPHA_PROMPT_20261007" in prompt else (
        "beta" if "AIG_PRIVATE_LOG_BETA_PROMPT_20261007" in prompt else "single"
    )
    attempt = getattr(_state, "attempt", 0)
    _state.attempt = attempt + 1
    if os.environ.get("AIG_LOG_HOLD") == "1":
        entered = Path(os.environ["AIG_LOG_ENTERED"])
        release = Path(os.environ["AIG_LOG_RELEASE"])
        entered.touch(exist_ok=True)
        deadline = time.monotonic() + 30
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not release.exists():
            raise TimeoutError("fixture release barrier timed out")
        output = _state.output_name
        return {"choices": [{"message": {"content":
            '/exec {"outputs":{"' + output + '":"AIG_PRIVATE_LOG_OUTPUT_20261007"}}'
        }}]}
    if os.environ.get("AIG_LOG_CONCURRENT") == "1" and attempt == 0:
        directory = Path(os.environ["AIG_LOG_CONCURRENT_DIR"])
        marker = directory / (flavor + ".entered")
        marker.touch(exist_ok=True)
        deadline = time.monotonic() + 20
        while not all((directory / (name + ".entered")).exists()
                      for name in ("alpha", "beta")) and time.monotonic() < deadline:
            time.sleep(0.01)
        if not all((directory / (name + ".entered")).exists()
                   for name in ("alpha", "beta")):
            raise TimeoutError("concurrent fixture barrier timed out")
        if flavor == "beta":
            time.sleep(0.05)
        raise ProviderError(503 if flavor == "alpha" else 429,
                            "AIG_PRIVATE_LOG_PROVIDER_20261007")
    if attempt == 0:
        raise ProviderError(503, "AIG_PRIVATE_LOG_PROVIDER_20261007")
    output = {"alpha": "alpha_result", "beta": "beta_result"}.get(
        flavor, _state.output_name
    )
    value = {"alpha": "AIG_PRIVATE_LOG_ALPHA_OUTPUT_20261007",
             "beta": "AIG_PRIVATE_LOG_BETA_OUTPUT_20261007"}.get(
                 flavor, "AIG_PRIVATE_LOG_OUTPUT_20261007")
    return {"choices": [{"message": {"content":
        '/exec {"outputs":{"' + output + '":"' + value + '"}}'
    }}]}

class Worker:
    def __init__(self, worker_id):
        self.worker_id = worker_id
        self._llm = LLMWorker(
            model="AIG_PRIVATE_LOG_PROMPT_20261007",
            api_key="AIG_PRIVATE_LOG_KEY_20261007",
            base_url="http://fixture.invalid/v1",
            worker_id=worker_id,
            transport=_transport,
            max_retries=1,
            retry_backoff=0,
        )

    def invoke(self, contract, disclosed_assets):
        _state.attempt = 0
        _state.output_name = contract.outputs[0]
        return self._llm.invoke(contract, disclosed_assets)

def build_worker(*, worker_id):
    return Worker(worker_id)
"""

_DB_FINGERPRINT = r"""
import hashlib, json, sqlite3, sys
from pathlib import Path
path = Path(sys.argv[1]).resolve()
if not path.exists():
    print(json.dumps({"exists": False}))
    raise SystemExit(0)
db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
try:
    db.execute("BEGIN")
    tables = {}
    table_hashes = {}
    semantic_table_hashes = {}
    names = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    for name in names:
        q = name.replace('"', '""')
        cursor = db.execute(f'SELECT * FROM "{q}"')
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        normalized = [[v.hex() if isinstance(v, bytes) else v for v in row] for row in rows]
        normalized.sort(key=lambda x: json.dumps(x, sort_keys=True, default=str))
        tables[name] = normalized
        table_hashes[name] = hashlib.sha256(json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), default=str
        ).encode()).hexdigest()
        ignored = ({"created_at", "updated_at"} if name == "worker_claims" else
                   {"updated_at"} if name == "worker_registrations" else set())
        semantic_indexes = [i for i, column in enumerate(columns) if column not in ignored]
        semantic_rows = [[row[i] for i in semantic_indexes] for row in normalized]
        semantic_rows.sort(key=lambda x: json.dumps(x, sort_keys=True, default=str))
        semantic_table_hashes[name] = hashlib.sha256(json.dumps(
            semantic_rows, sort_keys=True, separators=(",", ":"), default=str
        ).encode()).hexdigest()
    payload = json.dumps(tables, sort_keys=True, separators=(",", ":"), default=str)
    print(json.dumps({"exists": True, "table_names": names,
        "row_counts": {name: len(rows) for name, rows in tables.items()},
        "table_sha256": table_hashes,
        "table_semantic_sha256": semantic_table_hashes, "revision": max(
        [r[0] for r in db.execute("SELECT revision FROM runtime_records")] or [0]
    ), "sha256": hashlib.sha256(payload.encode()).hexdigest()}))
finally:
    db.close()
"""

_REOPEN_ONLY = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
def records(store):
    return sorted((record.id, record.recorded_at) for _, record in store.scan_runtime_records())
first = SQLiteStore.snapshot_from_path(sys.argv[1])
before = records(first)
first.close()
second = SQLiteStore.snapshot_from_path(sys.argv[1])
after = records(second)
second.close()
print(json.dumps({"same_records": before == after, "count": len(before)}))
"""

_REOPEN_REBUILD = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
path = sys.argv[1]
def records(store):
    return sorted((r.id, r.recorded_at) for _, r in store.scan_runtime_records())
store = SQLiteStore(path)
before = records(store)
before_digest = store.runtime_materialization_digest()
first = store.rebuild_runtime_materializations()
after = records(store)
after_digest = store.runtime_materialization_digest()
store.close()
store = SQLiteStore(path)
reopened = records(store)
reopened_digest = store.runtime_materialization_digest()
second = store.rebuild_runtime_materializations()
final = records(store)
final_digest = store.runtime_materialization_digest()
store.close()
print(json.dumps({"same_records": before == after == reopened == final,
                  "stable_digest": before_digest == first == after_digest == reopened_digest == second == final_digest,
                  "before_digest": before_digest, "first_digest": first,
                  "after_digest": after_digest, "reopened_digest": reopened_digest,
                  "second_digest": second, "final_digest": final_digest,
                  "count": len(before),
                  "sqlite_reopened": True}))
"""

_REDIS_QUERY_REBUILD = r"""
import json, sys
from aigineering.application import collaboration_service, query_projection
from aigineering.core.sqlite_store import SQLiteStore
store = SQLiteStore(sys.argv[1])
try:
    relay = collaboration_service(store).rebuild()
    query = query_projection(store).rebuild()
    current = collaboration_service(store).status().get("current")
finally:
    store.close()
store = SQLiteStore(sys.argv[1])
try:
    reopened_current = collaboration_service(store).status().get("current")
finally:
    store.close()
print(json.dumps({"redis_rebuild": relay, "redis_current": current,
                  "redis_reopened_current": reopened_current,
                  "query_digest": query.digest}))
"""

_RUNS_FOR_CONTRACT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.task_diagnostics import task_runs
store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    result = task_runs(store, sys.argv[2], limit=100)
    print(json.dumps(result, sort_keys=True))
finally:
    store.close()
"""

_RUNS_FOR_NAMED_CONTRACT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.task_diagnostics import task_runs
store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    selected = [c for c in store.get_all_contracts() if c.name == sys.argv[2]]
    if len(selected) != 1:
        raise RuntimeError("expected one fixture Contract")
    print(json.dumps({"contract_id": selected[0].id,
                      "runs": task_runs(store, selected[0].id, limit=100)}, sort_keys=True))
finally:
    store.close()
"""

_PUBLISH_CONCURRENT_TASKS = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_runtime_publishers
from aigineering.task_publication import prepare_task_contract, publish_task_contract
db_path = sys.argv[1]
store = SQLiteStore(db_path)
try:
    ensure_local_runtime_publishers(store)
    result = []
    for name, description, output in (
        ("installed-log-shared-alpha", "AIG_PRIVATE_LOG_ALPHA_PROMPT_20261007", "alpha_result"),
        ("installed-log-shared-beta", "AIG_PRIVATE_LOG_BETA_PROMPT_20261007", "beta_result"),
    ):
        contract = prepare_task_contract({"name": name, "description": description,
                                          "inputs": (), "activation": "", "budget": 2,
                                          "outputs": (output,)})
        result.append(publish_task_contract(store, contract).id)
finally:
    store.close()
print(json.dumps({"contract_ids": result}))
"""

_SHARED_HOST_FLEET = r"""
from concurrent.futures import ThreadPoolExecutor
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.fleet_config import build_fleet_worker, load_fleet_config
from aigineering.local_fleet import FleetHost, run_local_fleet
from aigineering.local_identity import ensure_local_runtime_publishers, ensure_local_worker_host
db_path, config_path, alpha, beta = sys.argv[1:]
store = SQLiteStore(db_path)
try:
    ensure_local_runtime_publishers(store)
    spec = load_fleet_config(config_path).workers[0]
    adapter = build_fleet_worker(spec)
    host = ensure_local_worker_host(store, adapter)
finally:
    store.close()
def execute(contract_id):
    result = run_local_fleet(db_path, (FleetHost(host, capacity=1),),
                             target_contract_id=contract_id, timeout=30,
                             poll_interval=0.01)
    return {"contract_id": result.contract_id, "status": result.status,
            "completed": result.completed, "timed_out": result.timed_out,
            "worker_errors": list(result.worker_errors),
            "capture_warnings": list(result.capture_warnings)}
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(execute, (alpha, beta)))
print(json.dumps({"results": results}, sort_keys=True))
"""

_CONFLICT_CAPTURE_RUN = r"""
import json, sys
from pathlib import Path
from aigineering.core.domain import load_genesis
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.fleet_config import build_fleet_worker, load_fleet_config
from aigineering.local_identity import ensure_local_runtime_publishers, ensure_local_worker_host
from aigineering.runtime_services import claim_next_application_package
from aigineering.task_diagnostics import task_runs
from aigineering.task_logs import capture_path, execute_captured_package, store_log_root
from aigineering.task_publication import prepare_task_contract, publish_task_contract
db_path, fleet_path = sys.argv[1:]
store = SQLiteStore(db_path)
try:
    publishers = ensure_local_runtime_publishers(store)
    contract = prepare_task_contract({"name": "installed-log-capture-conflict",
        "description": "A signed task for the capture-conflict control.",
        "inputs": (), "activation": "", "budget": 2, "outputs": ("conflict_result",)})
    contract = publish_task_contract(store, contract)
    spec = load_fleet_config(fleet_path).workers[0]
    worker = build_fleet_worker(spec)
    host = ensure_local_worker_host(store, worker)
    genesis = load_genesis(store)
    claimed = claim_next_application_package(store, worker_id=host.worker_id,
        contract_id=contract.id, candidate_publishers=publishers,
        claim_candidate=host.claim_candidate(request_id="installed-log-conflict-claim",
                                              contract_id=contract.id),
        expected_registration_version=host.registration_version)
    if claimed is None:
        raise RuntimeError("signed fixture claim was not acquired")
    root = store_log_root(db_path)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = capture_path(root, genesis.id, claimed.package.claim_id)
    path.symlink_to(path.with_suffix(".private-target"))
    warnings = []
    execute_captured_package(claimed, host, store, db_path=db_path,
        candidate_publishers=publishers, warnings=warnings)
    runs = task_runs(store, contract.id, limit=100)
    print(json.dumps({"contract_id": contract.id, "claim_id": claimed.package.claim_id,
        "capture_warnings": warnings, "runs": runs,
        "conflict_path_is_symlink": path.is_symlink(),
        "target_exists": path.with_suffix(".private-target").exists()}, sort_keys=True))
finally:
    store.close()
"""


def _exclusive(path: Path, data: str | bytes) -> None:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def installed_task_logs_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Produce and inspect an actual installed-runtime capture, read-only."""
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    if not redis_url or not redis_url.rsplit("/", 1)[-1].isdigit():
        raise RuntimeError("explicit isolated Redis database URL is required")
    root = cwd / f"installed-task-logs-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    custom_store_dir = root / "custom"
    custom_store_dir.mkdir(mode=0o700)
    custom_db = custom_store_dir / "runtime.sqlite"
    evidence = root / "private-command-streams"
    evidence.mkdir(mode=0o700)
    fixtures = root / "fixtures"
    fixtures.mkdir(mode=0o700)
    identity = root / ".aig" / "identity"
    identity.mkdir(parents=True, mode=0o700)
    _exclusive(fixtures / "log_worker.py", _FIXTURE)

    local_env = dict(env)
    local_env["AIGINEERING_REDIS_URL"] = redis_url
    local_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")
    local_env["PYTHONPATH"] = str(fixtures)
    for name in (
        "AIGINEERING_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
    ):
        local_env.pop(name, None)

    commands: list[dict] = []
    active_phase = "setup"

    def retain(label: str, argv: list[str], completed, expected: int = 0):
        n = len(commands) + 1
        out_path = evidence / f"{n:03d}-{label}-stdout.raw"
        err_path = evidence / f"{n:03d}-{label}-stderr.raw"
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", errors="surrogateescape")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8", errors="surrogateescape")
        _exclusive(out_path, stdout)
        _exclusive(err_path, stderr)
        commands.append(
            {
                "label": label,
                "argv": argv,
                "exit_code": completed.returncode,
                "stdout_file": out_path.name,
                "stderr_file": err_path.name,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            }
        )
        for marker in _MARKERS:
            marker_bytes = marker.encode("utf-8")
            if marker_bytes in stdout or marker_bytes in stderr:
                _exclusive(
                    evidence / f"{n:03d}-{label}-marker-leak.txt",
                    "private marker detected\n",
                )
                raise RuntimeError(
                    f"private output marker detected in {label}; evidence retained"
                )
        if completed.returncode != expected:
            raise RuntimeError(
                f"{label} exit {completed.returncode}, expected {expected}; evidence retained"
            )

    def run(
        label: str,
        argv: list[str],
        expected: int = 0,
        parse_json: bool = True,
        selected_env: dict[str, str] | None = None,
        timeout: float = 90,
    ):
        nonlocal active_phase
        active_phase = label
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=selected_env or local_env,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = subprocess.CompletedProcess(
                argv, -signal.SIGKILL, exc.stdout or b"", exc.stderr or b""
            )
            retain(label + "-timeout", argv, timed_out, -signal.SIGKILL)
            raise RuntimeError(
                f"{label} exceeded its bounded command timeout"
            ) from None
        retain(label, argv, completed, expected)
        if not parse_json:
            return completed, None
        try:
            return completed, json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{label} did not return JSON; evidence retained"
            ) from exc

    def cli(label: str, *args: str, expected: int = 0):
        return run(label, [str(aig), "-C", str(root), *args, "--json"], expected)

    def db_fingerprint(label: str, db_path: Path | None = None):
        _cp, result = run(
            label,
            [
                str(python),
                "-c",
                _DB_FINGERPRINT,
                str(db_path or custom_db),
            ],
        )
        return result

    def readonly_cli(label: str, *args: str, expected: int = 0):
        before = db_fingerprint(f"{label}-db-before")
        cp, result = cli(label, *args, expected=expected)
        after = db_fingerprint(f"{label}-db-after")
        if before != after:
            raise RuntimeError(f"{label} changed SQLite facts; evidence retained")
        return cp, result

    try:
        worker_toml = fixtures / "workers.toml"
        preset = fixtures / "run.toml"
        source = fixtures / "source.txt"
        _exclusive(source, "AIG_PRIVATE_LOG_PROMPT_20261007\n")
        _exclusive(
            worker_toml,
            '[fleet]\ndb_path = "custom/runtime.sqlite"\npoll_interval = 0.01\n'
            '[[workers]]\nid = "installed-log-smoke-worker"\nkind = "local"\ncapacity = 2\n'
            'worker_factory = "log_worker:build_worker"\n',
        )
        _exclusive(
            preset,
            f'[run]\nfleet = "{worker_toml.name}"\n'
            '[[inputs]]\nname = "source"\npath = "source.txt"\n'
            '[task]\nname = "installed-log-smoke"\n'
            'description = "A signed local fixture for invocation log inspection."\n'
            'inputs = ["source"]\nactivation = "source"\nbudget = 2\noutputs = ["result"]\n',
        )

        _cp, run_result = cli("signed-local-run", "run", "--config", str(preset))
        contract_id = (
            run_result.get("contract_id") if isinstance(run_result, dict) else None
        )
        if (
            not isinstance(run_result, dict)
            or not isinstance(contract_id, str)
            or run_result.get("completed") is not True
            or run_result.get("timed_out") is not False
            or run_result.get("status") != "complete"
            or run_result.get("worker_errors") != []
            or run_result.get("capture_warnings") != []
        ):
            raise RuntimeError(
                "installed Worker fixture did not complete; evidence retained"
            )
        _cp, runs_result = run(
            "task-runs-custom-store",
            [str(python), "-c", _RUNS_FOR_CONTRACT, str(custom_db), contract_id],
        )
        rows = runs_result.get("data", {}).get("runs", [])
        if len(rows) != 1 or rows[0].get("status") != "submitted":
            raise RuntimeError("signed run did not produce one submitted claim")
        claim_id = rows[0].get("claim_id")
        if not isinstance(claim_id, str):
            raise RuntimeError("run view omitted its exact claim ID")
        initial_fingerprint = db_fingerprint("initial-custom-store-fingerprint")
        expected_tables = {
            "asset_contents",
            "asset_definition_content_assertions",
            "asset_definitions",
            "assets",
            "claims",
            "contract_activation_refs",
            "contract_declared_outputs",
            "contracts",
            "idempotency_records",
            "replacement_claims",
            "runtime_lifecycle",
            "runtime_records",
            "schema_version",
            "sessions",
            "sqlite_sequence",
            "trace_events",
            "worker_claims",
            "worker_registrations",
        }
        if (
            initial_fingerprint.get("exists") is not True
            or set(initial_fingerprint.get("table_names", [])) != expected_tables
            or len(initial_fingerprint.get("table_names", [])) != 18
            or set(initial_fingerprint.get("row_counts", {})) != expected_tables
            or set(initial_fingerprint.get("table_sha256", {})) != expected_tables
            or set(initial_fingerprint.get("table_semantic_sha256", {}))
            != expected_tables
        ):
            raise RuntimeError(
                "custom Store fingerprint table inventory differs from the 18-table schema"
            )

        cursor = None
        first_cursor = None
        last_data = None
        events = []
        page_count = 0
        while True:
            args = [
                "task",
                "logs",
                contract_id,
                "--claim",
                claim_id,
                "--store",
                str(custom_db),
                "--limit",
                "1",
            ]
            if cursor:
                args += ["--cursor", cursor]
            _cp, page = readonly_cli(f"logs-page-{page_count + 1}", *args)
            data = page.get("data", {})
            if page.get("ok") is not True or data.get("claim_id") != claim_id:
                raise RuntimeError("logs page did not bind the exact claim")
            events.extend(data.get("events", []))
            page_count += 1
            cursor = data.get("next_cursor")
            if first_cursor is None:
                first_cursor = cursor
            last_data = data
            if data.get("truncated") is False:
                if not isinstance(cursor, str):
                    raise RuntimeError(
                        "complete log page omitted its resumable continuation cursor"
                    )
                if data.get("truncated") is not False:
                    raise RuntimeError(
                        "final logs page reports an incomplete cursor boundary"
                    )
                if data.get("follow_status") != "complete":
                    raise RuntimeError(
                        "completed Worker invocation not reflected by log status"
                    )
                break
            if not isinstance(cursor, str):
                raise RuntimeError("truncated log page omitted its continuation cursor")
            if page_count > 100:
                raise RuntimeError("cursor pagination exceeded bounded capture pages")

        names = [event.get("event") for event in events]
        if not {
            "invocation.started",
            "provider.retry",
            "candidate.returned",
            "invocation.finished",
        } <= set(names):
            raise RuntimeError(
                "captured events omit actual LLM retry or invocation boundary"
            )
        if any(
            any(marker in json.dumps(event, sort_keys=True) for marker in _MARKERS)
            for event in events
        ):
            raise RuntimeError("captured event view exposed a private fixture marker")
        capture_domain = last_data.get("domain_id") if last_data else None
        if not isinstance(capture_domain, str):
            raise RuntimeError("capture page omitted its validated domain ID")
        capture_name = (
            hashlib.sha256((capture_domain + "\0" + claim_id).encode()).hexdigest()[:32]
            + ".jsonl"
        )
        raw_capture = (custom_store_dir / "worker-logs" / capture_name).read_bytes()
        _exclusive(evidence / "primary-capture.raw", raw_capture)
        if any(marker.encode("utf-8") in raw_capture for marker in _MARKERS):
            raise RuntimeError("private fixture marker appeared in raw capture bytes")

        _cp, conflict = run(
            "capture-symlink-conflict-control",
            [
                str(python),
                "-c",
                _CONFLICT_CAPTURE_RUN,
                str(custom_db),
                str(worker_toml),
            ],
        )
        conflict_contract = conflict.get("contract_id")
        conflict_claim = conflict.get("claim_id")
        conflict_rows = conflict.get("runs", {}).get("data", {}).get("runs", [])
        if (
            not isinstance(conflict_contract, str)
            or not isinstance(conflict_claim, str)
            or conflict.get("capture_warnings") != ["capture_conflict"]
            or conflict.get("conflict_path_is_symlink") is not True
            or conflict.get("target_exists") is not False
            or len(conflict_rows) != 1
            or conflict_rows[0].get("status") != "submitted"
        ):
            raise RuntimeError("capture symlink conflict changed signed execution")
        _cp, conflict_read = readonly_cli(
            "capture-symlink-conflict-read",
            "task",
            "logs",
            conflict_contract,
            "--claim",
            conflict_claim,
            "--store",
            str(custom_db),
            expected=1,
        )
        if conflict_read != {"ok": False, "error": "capture_unsafe_path"}:
            raise RuntimeError("capture reader followed or misclassified a symlink")

        default_store = root / ".aig" / "store.db"
        wrong_store = root / "wrong-store" / "missing.sqlite"
        if default_store.exists() or wrong_store.exists():
            raise RuntimeError("store isolation fixture path unexpectedly exists")
        for label, extra in (
            ("default-store-no-fallback", ()),
            ("wrong-store-no-fallback", ("--store", str(wrong_store))),
        ):
            _cp, isolated_error = readonly_cli(
                label,
                "task",
                "logs",
                contract_id,
                "--claim",
                claim_id,
                *extra,
                expected=1,
            )
            if isolated_error != {"ok": False, "error": "inspection_failed"}:
                raise RuntimeError(f"{label} unexpectedly resolved the custom Store")
            if default_store.exists() or wrong_store.exists():
                raise RuntimeError(f"{label} created a Store during read-only lookup")

        follow_args = [
            str(aig),
            "-C",
            str(root),
            "task",
            "logs",
            contract_id,
            "--claim",
            claim_id,
            "--store",
            str(custom_db),
            "--cursor",
            first_cursor,
            "--follow",
            "--timeout",
            "0.2",
            "--json",
        ]
        static_follow_before = db_fingerprint("static-follow-db-before")
        follow_cp, _ = run("follow-jsonl", follow_args, parse_json=False)
        static_follow_after = db_fingerprint("static-follow-db-after")
        if static_follow_before != static_follow_after:
            raise RuntimeError("static follow changed SQLite facts")
        follow_rows = [json.loads(line) for line in follow_cp.stdout.splitlines()]
        if (
            not follow_rows
            or follow_rows[-1].get("type") != "end"
            or follow_rows[-1].get("status") != "timeout"
            or any(row.get("type") == "end" for row in follow_rows[:-1])
            or any(
                marker in json.dumps(row, sort_keys=True)
                for row in follow_rows
                for marker in _MARKERS
            )
        ):
            raise RuntimeError("follow JSONL did not end at a safe timeout boundary")

        interrupted_argv = [
            str(aig),
            "-C",
            str(root),
            "task",
            "logs",
            contract_id,
            "--claim",
            claim_id,
            "--store",
            str(custom_db),
            "--follow",
            "--timeout",
            "5",
            "--json",
        ]
        interrupt_before = db_fingerprint("interrupt-follow-db-before")
        interrupted_process = subprocess.Popen(
            interrupted_argv,
            cwd=cwd,
            env=local_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.3)
        interrupted_process.send_signal(signal.SIGINT)
        try:
            interrupted_stdout, interrupted_stderr = interrupted_process.communicate(
                timeout=5
            )
        except subprocess.TimeoutExpired as exc:
            interrupted_process.kill()
            try:
                tail_out, tail_err = interrupted_process.communicate(timeout=5)
            except subprocess.TimeoutExpired as cleanup_exc:
                tail_out = cleanup_exc.stdout or b""
                tail_err = cleanup_exc.stderr or b""
            interrupted_stdout = tail_out or exc.stdout or b""
            interrupted_stderr = tail_err or exc.stderr or b""
        interrupted_completed = subprocess.CompletedProcess(
            interrupted_argv,
            interrupted_process.returncode,
            interrupted_stdout,
            interrupted_stderr,
        )
        retain("follow-interrupted", interrupted_argv, interrupted_completed, 0)
        interrupt_after = db_fingerprint("interrupt-follow-db-after")
        interrupted_rows = [
            json.loads(line) for line in interrupted_stdout.splitlines() if line.strip()
        ]
        if (
            interrupt_before != interrupt_after
            or not interrupted_rows
            or interrupted_rows[-1].get("type") != "end"
            or interrupted_rows[-1].get("status") != "interrupted"
        ):
            raise RuntimeError(
                "follow interruption was not local, observable and read-only"
            )

        # A tampered cursor must fail safely and must not write SQLite facts.
        if not isinstance(first_cursor, str):
            raise RuntimeError("capture did not expose a pagination cursor")
        cursor_bytes = bytearray(
            base64.urlsafe_b64decode(first_cursor + "=" * (-len(first_cursor) % 4))
        )
        cursor_bytes[-1] ^= 1
        bad_cursor = base64.urlsafe_b64encode(cursor_bytes).decode().rstrip("=")
        _cp, bad_cursor_result = readonly_cli(
            "tampered-cursor",
            "task",
            "logs",
            contract_id,
            "--claim",
            claim_id,
            "--store",
            str(custom_db),
            "--cursor",
            bad_cursor,
            expected=1,
        )
        if bad_cursor_result != {"ok": False, "error": "cursor_invalid"}:
            raise RuntimeError("tampered log cursor was not rejected safely")

        # Corrupt only this temporary capture, retaining original bytes privately.
        domain_id = last_data.get("domain_id") if isinstance(last_data, dict) else None
        if not isinstance(domain_id, str):
            raise RuntimeError("log page omitted its domain binding")
        capture_name = (
            hashlib.sha256((domain_id + "\0" + claim_id).encode()).hexdigest()[:32]
            + ".jsonl"
        )
        capture = custom_store_dir / "worker-logs" / capture_name
        original_capture = capture.read_bytes()
        _exclusive(evidence / "capture-before-corruption.raw", original_capture)
        if not original_capture:
            raise RuntimeError("capture file was unexpectedly empty")
        if any(marker.encode("utf-8") in original_capture for marker in _MARKERS):
            raise RuntimeError("private fixture marker appeared in raw capture bytes")
        raw_lines = original_capture.splitlines(keepends=True)
        header = json.loads(raw_lines[0])
        original_hash = header["hash"]
        header["hash"] = ("0" if original_hash[0] != "0" else "1") + original_hash[1:]
        raw_lines[0] = (
            json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        capture.write_bytes(b"".join(raw_lines))
        _cp, corrupt_result = readonly_cli(
            "corrupt-capture",
            "task",
            "logs",
            contract_id,
            "--claim",
            claim_id,
            "--store",
            str(custom_db),
            expected=1,
        )
        if corrupt_result != {"ok": False, "error": "capture_corrupt"}:
            raise RuntimeError("corrupt private capture was not rejected safely")
        capture.write_bytes(original_capture)
        _exclusive(evidence / "capture-before-partial-tail.raw", original_capture)
        with capture.open("ab") as stream:
            stream.write(b'{"partial":"')
        _cp, incomplete_result = readonly_cli(
            "partial-tail-capture",
            "task",
            "logs",
            contract_id,
            "--claim",
            claim_id,
            "--store",
            str(custom_db),
            expected=1,
        )
        if incomplete_result != {"ok": False, "error": "capture_incomplete"}:
            raise RuntimeError(
                "partial capture tail was not distinguished from corruption"
            )
        capture.write_bytes(original_capture)

        # Hold a second ordinary signed invocation inside the fake transport.
        # The public follow command must observe a real start before release,
        # then observe the finish after the Worker returns.
        live_preset = fixtures / "live-run.toml"
        _exclusive(
            live_preset,
            f'[run]\nfleet = "{worker_toml.name}"\n'
            '[[inputs]]\nname = "source"\npath = "source.txt"\n'
            '[task]\nname = "installed-log-live-follow"\n'
            'description = "A held signed invocation for live follow inspection."\n'
            'inputs = ["source"]\nactivation = "source"\nbudget = 2\noutputs = ["live_result"]\n',
        )
        entered = root / "fixture-entered"
        release = root / "fixture-release"
        live_env = dict(local_env)
        live_env["AIG_LOG_HOLD"] = "1"
        live_env["AIG_LOG_ENTERED"] = str(entered)
        live_env["AIG_LOG_RELEASE"] = str(release)
        live_argv = [
            str(aig),
            "-C",
            str(root),
            "run",
            "--config",
            str(live_preset),
            "--json",
        ]
        live_process = subprocess.Popen(
            live_argv,
            cwd=cwd,
            env=live_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        active_phase = "live-held-run"
        follow_process = None
        live_recorded = False
        follow_recorded = False
        follow_prefix = b""
        try:
            deadline = time.monotonic() + 20
            while not entered.exists() and time.monotonic() < deadline:
                if live_process.poll() is not None:
                    break
                time.sleep(0.02)
            if not entered.exists():
                raise RuntimeError("held Worker did not enter the fake transport")
            _cp, live_lookup = run(
                "live-task-exact-claim-proof",
                [
                    str(python),
                    "-c",
                    _RUNS_FOR_NAMED_CONTRACT,
                    str(custom_db),
                    "installed-log-live-follow",
                ],
            )
            live_contract = live_lookup.get("contract_id")
            live_runs = live_lookup.get("runs", {})
            live_rows = [
                row
                for row in live_runs.get("data", {}).get("runs", [])
                if row.get("worker_id") == "installed-log-smoke-worker"
                and row.get("status") == "active"
            ]
            if len(live_rows) != 1:
                raise RuntimeError("could not identify the one live signed claim")
            live_claim = live_rows[0].get("claim_id")
            if not isinstance(live_claim, str):
                raise RuntimeError("live claim diagnostics omitted claim ID")

            before_follow = db_fingerprint("live-follow-sqlite-before")
            follow_argv = [
                str(aig),
                "-C",
                str(root),
                "task",
                "logs",
                live_contract,
                "--claim",
                live_claim,
                "--store",
                str(custom_db),
                "--follow",
                "--timeout",
                "5",
                "--json",
            ]
            follow_process = subprocess.Popen(
                follow_argv,
                cwd=cwd,
                env=local_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            active_phase = "live-follow-jsonl"
            first_line = None
            ready, _, _ = select.select([follow_process.stdout], [], [], 8)
            if ready:
                first_line = follow_process.stdout.readline()
            if not first_line:
                raise RuntimeError(
                    "live follow produced no event while invocation was held"
                )
            follow_prefix = first_line
            first_event = json.loads(first_line)
            if (
                first_event.get("type") != "event"
                or first_event.get("data", {}).get("event") != "invocation.started"
            ):
                raise RuntimeError("live follow did not expose the exact started event")
            if db_fingerprint("live-follow-sqlite-during") != before_follow:
                raise RuntimeError("live follow changed SQLite facts")
            _exclusive(release, b"")
            try:
                live_stdout, live_stderr = live_process.communicate(timeout=30)
            except subprocess.TimeoutExpired as exc:
                live_process.kill()
                try:
                    tail_out, tail_err = live_process.communicate(timeout=5)
                except subprocess.TimeoutExpired as cleanup_exc:
                    tail_out = cleanup_exc.stdout or b""
                    tail_err = cleanup_exc.stderr or b""
                live_stdout = tail_out or exc.stdout or b""
                live_stderr = tail_err or exc.stderr or b""
                live_completed = subprocess.CompletedProcess(
                    live_argv, live_process.returncode, live_stdout, live_stderr
                )
                retain(
                    "live-held-run-timeout", live_argv, live_completed, -signal.SIGKILL
                )
                live_recorded = True
                raise RuntimeError("live held run exceeded its bounded timeout")
            live_completed = subprocess.CompletedProcess(
                live_argv, live_process.returncode, live_stdout, live_stderr
            )
            retain("live-held-run", live_argv, live_completed, 0)
            live_recorded = True

            try:
                follow_tail, follow_stderr = follow_process.communicate(timeout=8)
            except subprocess.TimeoutExpired as exc:
                follow_process.kill()
                try:
                    tail_out, tail_err = follow_process.communicate(timeout=5)
                except subprocess.TimeoutExpired as cleanup_exc:
                    tail_out = cleanup_exc.stdout or b""
                    tail_err = cleanup_exc.stderr or b""
                follow_tail = tail_out or exc.stdout or b""
                follow_stderr = tail_err or exc.stderr or b""
                follow_code = follow_process.returncode
                follow_completed = subprocess.CompletedProcess(
                    follow_argv, follow_code, follow_prefix + follow_tail, follow_stderr
                )
                retain(
                    "live-follow-timeout",
                    follow_argv,
                    follow_completed,
                    -signal.SIGKILL,
                )
                follow_recorded = True
                raise RuntimeError("live follow exceeded its bounded timeout")
            follow_code = follow_process.returncode
            follow_completed = subprocess.CompletedProcess(
                follow_argv, follow_code, first_line + follow_tail, follow_stderr
            )
            retain("live-follow-jsonl", follow_argv, follow_completed, 0)
            follow_recorded = True
            live_follow_rows = [
                json.loads(line) for line in follow_completed.stdout.splitlines()
            ]
            live_event_names = [
                row.get("data", {}).get("event")
                for row in live_follow_rows
                if row.get("type") == "event"
            ]
            if (
                "invocation.started" not in live_event_names
                or "invocation.finished" not in live_event_names
                or live_follow_rows[-1].get("type") != "end"
                or live_follow_rows[-1].get("status") != "timeout"
            ):
                raise RuntimeError("live follow missed invocation boundary events")
            live_result = json.loads(live_completed.stdout)
            if (
                not isinstance(live_result, dict)
                or live_result.get("completed") is not True
                or live_result.get("status") != "complete"
                or live_result.get("timed_out") is not False
                or live_result.get("worker_errors") != []
                or live_result.get("capture_warnings") != []
            ):
                raise RuntimeError("held signed invocation did not complete normally")
        finally:
            if not release.exists():
                _exclusive(release, b"")
            if live_process.poll() is None:
                live_process.kill()
            if not live_recorded:
                try:
                    stdout, stderr = live_process.communicate(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    stdout, stderr = exc.stdout or b"", exc.stderr or b""
                retain(
                    "live-held-run-unexpected",
                    live_argv,
                    subprocess.CompletedProcess(
                        live_argv, live_process.returncode, stdout, stderr
                    ),
                    live_process.returncode,
                )
            if follow_process is not None and not follow_recorded:
                if follow_process.poll() is None:
                    follow_process.kill()
                try:
                    stdout, stderr = follow_process.communicate(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    stdout, stderr = exc.stdout or b"", exc.stderr or b""
                retain(
                    "live-follow-unexpected",
                    follow_argv,
                    subprocess.CompletedProcess(
                        follow_argv,
                        follow_process.returncode,
                        follow_prefix + stdout,
                        stderr,
                    ),
                    follow_process.returncode,
                )

        # Two independent task roots run through the canonical Fleet service
        # concurrently while sharing exactly one WorkerHost and adapter object.
        _cp, concurrent_tasks = run(
            "shared-host-signed-task-publication",
            [str(python), "-c", _PUBLISH_CONCURRENT_TASKS, str(custom_db)],
        )
        task_ids = concurrent_tasks.get("contract_ids", [])
        if len(task_ids) != 2 or not all(isinstance(value, str) for value in task_ids):
            raise RuntimeError("shared-host task publication failed")
        alpha_contract, beta_contract = task_ids
        concurrent_dir = root / "shared-host-barrier"
        concurrent_dir.mkdir(mode=0o700)
        concurrent_env = dict(local_env)
        concurrent_env["AIG_LOG_CONCURRENT"] = "1"
        concurrent_env["AIG_LOG_CONCURRENT_DIR"] = str(concurrent_dir)
        _cp, fleet_pair = run(
            "shared-host-concurrent-fleet",
            [
                str(python),
                "-c",
                _SHARED_HOST_FLEET,
                str(custom_db),
                str(worker_toml),
                alpha_contract,
                beta_contract,
            ],
            selected_env=concurrent_env,
        )
        results = fleet_pair.get("results", [])
        if len(results) != 2 or any(
            item.get("completed") is not True
            or item.get("status") != "complete"
            or item.get("worker_errors") != []
            or item.get("capture_warnings") != []
            for item in results
        ):
            raise RuntimeError(
                "shared WorkerHost concurrent Fleet calls did not complete"
            )
        if not all(
            (concurrent_dir / (name + ".entered")).is_file()
            for name in ("alpha", "beta")
        ):
            raise RuntimeError(
                "shared adapter invocations did not overlap at the fake transport"
            )

        concurrency_event_statuses = {}
        alpha_claim_for_scope = None
        for label, task_id, expected_status in (
            ("alpha", alpha_contract, 503),
            ("beta", beta_contract, 429),
        ):
            _cp, attempt_view = run(
                f"shared-{label}-runs",
                [str(python), "-c", _RUNS_FOR_CONTRACT, str(custom_db), task_id],
            )
            attempt_rows = attempt_view.get("data", {}).get("runs", [])
            if len(attempt_rows) != 1 or attempt_rows[0].get("status") != "submitted":
                raise RuntimeError(f"shared {label} task lacks one submitted claim")
            exact_claim = attempt_rows[0].get("claim_id")
            if label == "alpha":
                alpha_claim_for_scope = exact_claim
            shared_cursor = None
            shared_events = []
            while True:
                page_args = [
                    "task",
                    "logs",
                    task_id,
                    "--claim",
                    exact_claim,
                    "--store",
                    str(custom_db),
                    "--limit",
                    "100",
                ]
                if shared_cursor:
                    page_args += ["--cursor", shared_cursor]
                _cp, shared_page = readonly_cli(f"shared-{label}-logs", *page_args)
                shared_data = shared_page.get("data", {})
                shared_events.extend(shared_data.get("events", []))
                shared_cursor = shared_data.get("next_cursor")
                if shared_data.get("truncated") is False:
                    break
                if not isinstance(shared_cursor, str):
                    raise RuntimeError("shared-host event page omitted cursor")
            retry_rows = [
                event
                for event in shared_events
                if event.get("event") == "provider.retry"
            ]
            if (
                len(retry_rows) != 1
                or retry_rows[0].get("fields", {}).get("status_code") != expected_status
            ):
                raise RuntimeError(
                    f"shared adapter event leaked across {label} capture"
                )
            if "invocation.finished" not in [
                event.get("event") for event in shared_events
            ]:
                raise RuntimeError(f"shared {label} capture missed invocation finish")
            capture_domain = shared_data.get("domain_id")
            if not isinstance(capture_domain, str):
                raise RuntimeError(f"shared {label} page omitted its domain")
            shared_capture_name = (
                hashlib.sha256(
                    (capture_domain + "\0" + exact_claim).encode()
                ).hexdigest()[:32]
                + ".jsonl"
            )
            raw_shared_capture = (
                custom_store_dir / "worker-logs" / shared_capture_name
            ).read_bytes()
            if any(marker.encode("utf-8") in raw_shared_capture for marker in _MARKERS):
                raise RuntimeError(f"private marker appeared in raw {label} capture")
            concurrency_event_statuses[label] = expected_status
        _cp, foreign_cursor_result = readonly_cli(
            "foreign-cursor-scope",
            "task",
            "logs",
            alpha_contract,
            "--claim",
            alpha_claim_for_scope,
            "--store",
            str(custom_db),
            "--cursor",
            cursor,
            expected=1,
        )
        if foreign_cursor_result != {"ok": False, "error": "cursor_scope_mismatch"}:
            raise RuntimeError("foreign task cursor was not rejected by exact scope")

        before_reopen = db_fingerprint("pre-close-reopen-fingerprint")
        _reopen_cp, reopen = run(
            "read-only-close-reopen",
            [str(python), "-c", _REOPEN_ONLY, str(custom_db)],
        )
        after_reopen = db_fingerprint("post-close-reopen-fingerprint")
        if before_reopen != after_reopen or reopen.get("same_records") is not True:
            raise RuntimeError("read-only Store close/reopen changed its full snapshot")
        before_materialization = db_fingerprint(
            "pre-sqlite-materialization-fingerprint"
        )
        _rebuild_cp, rebuild = run(
            "sqlite-materialization-rebuild",
            [str(python), "-c", _REOPEN_REBUILD, str(custom_db)],
        )
        after_materialization = db_fingerprint(
            "post-sqlite-materialization-fingerprint"
        )
        materialization_before_hashes = before_materialization.get("table_sha256", {})
        materialization_after_hashes = after_materialization.get("table_sha256", {})
        materialization_before_semantic = before_materialization.get(
            "table_semantic_sha256", {}
        )
        materialization_after_semantic = after_materialization.get(
            "table_semantic_sha256", {}
        )
        changed_materialization_tables = sorted(
            name
            for name in (
                materialization_before_hashes.keys()
                | materialization_after_hashes.keys()
            )
            if materialization_before_hashes.get(name)
            != materialization_after_hashes.get(name)
        )
        changed_materialization_semantic_tables = sorted(
            name
            for name in (
                materialization_before_semantic.keys()
                | materialization_after_semantic.keys()
            )
            if materialization_before_semantic.get(name)
            != materialization_after_semantic.get(name)
        )
        # SQLite materialization rebuild may normalize derived SQL rows; only
        # immutable record identity and stable materialization digest are its
        # oracle. The independent Redis/query rebuild below must leave all raw
        # SQLite tables byte-for-byte equivalent.
        active_phase = "sqlite-materialization-validation"
        if (
            rebuild.get("same_records") is not True
            or rebuild.get("stable_digest") is not True
            or changed_materialization_semantic_tables
            or set(changed_materialization_tables)
            - {"worker_claims", "worker_registrations"}
            or materialization_before_hashes.get("runtime_records")
            != materialization_after_hashes.get("runtime_records")
            or materialization_before_hashes.get("claims")
            != materialization_after_hashes.get("claims")
            or materialization_before_hashes.get("replacement_claims")
            != materialization_after_hashes.get("replacement_claims")
        ):
            raise RuntimeError(
                "SQLite materialization changed immutable records or semantic table fields"
            )
        redis_rebuild_before = db_fingerprint("pre-redis-rebuild-fingerprint")
        _redis_rebuild_cp, redis_rebuild = run(
            "isolated-redis-and-query-rebuild",
            [str(python), "-c", _REDIS_QUERY_REBUILD, str(custom_db)],
        )
        redis_rebuild_after = db_fingerprint("post-redis-rebuild-fingerprint")
        active_phase = "redis-query-validation"
        if (
            redis_rebuild_before != redis_rebuild_after
            or not isinstance(redis_rebuild.get("redis_rebuild"), dict)
            or redis_rebuild.get("redis_rebuild", {}).get("synced") is not True
            or redis_rebuild.get("redis_current") is not True
            or redis_rebuild.get("redis_reopened_current") is not True
            or not isinstance(redis_rebuild.get("query_digest"), str)
        ):
            raise RuntimeError(
                "Redis/query rebuild changed SQLite or lost its projection"
            )
        _cp, resumed_page = readonly_cli(
            "cursor-resume-after-reopen",
            "task",
            "logs",
            contract_id,
            "--claim",
            claim_id,
            "--store",
            str(custom_db),
            "--limit",
            "1",
            "--cursor",
            cursor,
        )
        resumed_data = resumed_page.get("data", {})
        if (
            resumed_page.get("ok") is not True
            or resumed_data.get("claim_id") != claim_id
            or resumed_data.get("events") != []
            or resumed_data.get("truncated") is not False
            or resumed_data.get("next_cursor") != cursor
        ):
            raise RuntimeError(
                "exact log cursor did not resume after Store reopen/rebuild"
            )

        quota_filler = custom_store_dir / "worker-logs" / "quota-pressure.jsonl"
        fd = os.open(quota_filler, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(fd, 64 * 1024 * 1024)
        finally:
            os.close(fd)
        quota_preset = fixtures / "quota-run.toml"
        _exclusive(
            quota_preset,
            f'[run]\nfleet = "{worker_toml.name}"\n'
            '[[inputs]]\nname = "source"\npath = "source.txt"\n'
            '[task]\nname = "installed-log-quota-control"\n'
            'description = "A signed local fixture under isolated log quota pressure."\n'
            'inputs = ["source"]\nactivation = "source"\nbudget = 2\noutputs = ["quota_result"]\n',
        )
        _cp, quota_result = cli(
            "quota-pressure-run", "run", "--config", str(quota_preset)
        )
        if (
            quota_result.get("completed") is not True
            or quota_result.get("status") != "complete"
            or "storage_limit" not in quota_result.get("capture_warnings", [])
            or quota_result.get("worker_errors") != []
        ):
            raise RuntimeError("log quota pressure changed the normal task result")
        quota_contract = quota_result.get("contract_id")
        _cp, quota_runs = run(
            "quota-task-runs-custom-store",
            [str(python), "-c", _RUNS_FOR_CONTRACT, str(custom_db), quota_contract],
        )
        quota_rows = quota_runs.get("data", {}).get("runs", [])
        if len(quota_rows) != 1 or not isinstance(quota_rows[0].get("claim_id"), str):
            raise RuntimeError("quota control omitted its exact signed claim")
        _cp, missing_capture = readonly_cli(
            "quota-capture-missing",
            "task",
            "logs",
            quota_contract,
            "--claim",
            quota_rows[0]["claim_id"],
            "--store",
            str(custom_db),
            expected=1,
        )
        if missing_capture != {"ok": False, "error": "capture_missing"}:
            raise RuntimeError("quota capture absence did not preserve unknown cause")
        missing_follow_before = db_fingerprint("missing-follow-db-before")
        missing_follow, _ = run(
            "missing-capture-follow",
            [
                str(aig),
                "-C",
                str(root),
                "task",
                "logs",
                quota_contract,
                "--claim",
                quota_rows[0]["claim_id"],
                "--store",
                str(custom_db),
                "--follow",
                "--timeout",
                "0.2",
                "--json",
            ],
            expected=1,
            parse_json=False,
        )
        missing_follow_after = db_fingerprint("missing-follow-db-after")
        if missing_follow_before != missing_follow_after:
            raise RuntimeError("missing-capture follow changed SQLite facts")
        missing_follow_row = json.loads(missing_follow.stdout)
        if missing_follow_row != {
            "ok": False,
            "error": "capture_missing",
            "type": "end",
            "status": "error",
        }:
            raise RuntimeError(
                "follow with no persisted capture did not exit with safe error"
            )

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dead_port = sock.getsockname()[1]
        outage_env = dict(local_env)
        outage_env["AIGINEERING_REDIS_URL"] = f"redis://127.0.0.1:{dead_port}/12"
        outage_before = db_fingerprint("redis-outage-db-before")
        _cp, outage = run(
            "required-redis-outage",
            [
                str(aig),
                "-C",
                str(root),
                "task",
                "logs",
                contract_id,
                "--claim",
                claim_id,
                "--store",
                str(custom_db),
                "--json",
            ],
            expected=1,
            selected_env=outage_env,
        )
        outage_after = db_fingerprint("redis-outage-db-after")
        if outage_before != outage_after:
            raise RuntimeError("Redis outage lookup changed SQLite facts")
        if outage != {
            "ok": False,
            "error": "redis_unavailable",
            "message": "Redis collaboration service unavailable; check Redis installation and health",
        }:
            raise RuntimeError(
                "Redis outage did not preserve the fixed safe preflight error"
            )

        permission_status = "not_testable_as_privileged_user"
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            denial_root = root / "permission-control"
            denial_fixtures = denial_root / "fixtures"
            denial_identity = denial_root / ".aig" / "identity"
            denial_fixtures.mkdir(parents=True, mode=0o700)
            denial_identity.mkdir(parents=True, mode=0o700)
            denial_logs = denial_root / ".aig" / "worker-logs"
            denial_logs.mkdir(mode=0o700)
            lock_path = denial_logs / ".quota.lock"
            _exclusive(lock_path, b"")
            os.chmod(lock_path, 0o400)
            _exclusive(denial_fixtures / "source.txt", "permission fixture input\n")
            denial_workers = denial_fixtures / "workers.toml"
            denial_preset = denial_fixtures / "run.toml"
            _exclusive(
                denial_workers,
                '[fleet]\ndb_path = ".aig/store.db"\npoll_interval = 0.01\n'
                '[[workers]]\nid = "installed-log-smoke-worker"\nkind = "local"\n'
                'worker_factory = "log_worker:build_worker"\n',
            )
            _exclusive(
                denial_preset,
                f'[run]\nfleet = "{denial_workers.name}"\n'
                '[[inputs]]\nname = "source"\npath = "source.txt"\n'
                '[task]\nname = "installed-log-permission-control"\n'
                'description = "A signed fixture for capture permission denial."\n'
                'inputs = ["source"]\nactivation = "source"\nbudget = 2\noutputs = ["permission_result"]\n',
            )
            denial_env = dict(local_env)
            denial_env["AIG_ACTOR_KEY_FILE"] = str(denial_identity / "root.ed25519")
            _cp, denial_result = run(
                "permission-denied-capture-run",
                [
                    str(aig),
                    "-C",
                    str(denial_root),
                    "run",
                    "--config",
                    str(denial_preset),
                    "--json",
                ],
                selected_env=denial_env,
            )
            if (
                denial_result.get("completed") is not True
                or denial_result.get("status") != "complete"
                or "capture_unavailable"
                not in denial_result.get("capture_warnings", [])
                or denial_result.get("worker_errors") != []
            ):
                raise RuntimeError(
                    "capture permission denial changed normal Worker result"
                )
            denial_contract = denial_result.get("contract_id")
            _cp, denial_runs = run(
                "permission-denied-task-runs",
                [
                    str(aig),
                    "-C",
                    str(denial_root),
                    "task",
                    "runs",
                    denial_contract,
                    "--json",
                ],
                selected_env=denial_env,
            )
            denial_rows = denial_runs.get("data", {}).get("runs", [])
            if len(denial_rows) != 1:
                raise RuntimeError(
                    "permission fixture did not preserve its signed claim"
                )
            denial_db = denial_root / ".aig/store.db"
            before_denial_read = db_fingerprint("permission-read-db-before", denial_db)
            _cp, denial_log = run(
                "permission-denied-capture-read",
                [
                    str(aig),
                    "-C",
                    str(denial_root),
                    "task",
                    "logs",
                    denial_contract,
                    "--claim",
                    denial_rows[0]["claim_id"],
                    "--store",
                    str(denial_db),
                    "--json",
                ],
                expected=1,
                selected_env=denial_env,
            )
            after_denial_read = db_fingerprint("permission-read-db-after", denial_db)
            if (
                denial_log != {"ok": False, "error": "capture_missing"}
                or before_denial_read != after_denial_read
            ):
                raise RuntimeError("permission capture absence was not safe/read-only")
            permission_status = "passed"

        return {
            "status": "passed",
            "contract_id": contract_id,
            "claim_id": claim_id,
            "capture_event_count": len(events),
            "capture_event_names": names,
            "page_count": page_count,
            "sqlite_read_only": True,
            "tampered_cursor_rejected": True,
            "isolated_capture_corruption_rejected": True,
            "live_follow": "passed",
            "shared_worker_host_concurrent_fleet": concurrency_event_statuses,
            "read_only_close_reopen": reopen["same_records"],
            "sqlite_materialization_rebuild": {
                "immutable_records_preserved": rebuild["same_records"],
                "deterministic_digest": rebuild["stable_digest"],
                "changed_table_names": changed_materialization_tables,
                "changed_semantic_table_names": changed_materialization_semantic_tables,
            },
            "redis_query_rebuild_and_table_fingerprint": {
                "status": "passed",
                "redis_synced": redis_rebuild["redis_rebuild"]["synced"],
                "redis_current_after_reopen": redis_rebuild["redis_reopened_current"],
                "query_digest": redis_rebuild["query_digest"],
            },
            "quota_preserved_task_result": True,
            "required_redis_outage": True,
            "permission_denial": permission_status,
            "provider_calls": 0,
            "evidence_dir": str(evidence),
            "command_count": len(commands),
        }
    except Exception as exc:
        _exclusive(
            evidence / "failure.json",
            json.dumps(
                {
                    "phase": active_phase,
                    "error_type": type(exc).__name__,
                    "commands": commands,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        raise
    finally:
        _exclusive(
            evidence / "commands-final.json",
            json.dumps(commands, sort_keys=True, indent=2) + "\n",
        )
