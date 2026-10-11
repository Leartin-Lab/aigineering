"""Installed-CLI smoke for local named-board routing and domain isolation.

The caller installs the frozen wheel outside the source tree and supplies its
CLI/Python. This helper creates only disposable workspaces, records every child
process stream before checking it, and never invokes a Worker or provider.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid
from urllib.parse import urlsplit


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _exclusive(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _fingerprint_program() -> str:
    return r"""
import hashlib, json, sqlite3, sys
from pathlib import Path
path = Path(sys.argv[1]).resolve()
conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
try:
    conn.execute("BEGIN")
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    tables = {}
    for name in names:
        q = '"' + name.replace('"', '""') + '"'
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({q})")]
        rows = conn.execute(f"SELECT * FROM {q}").fetchall()
        clean = [[v.hex() if isinstance(v, bytes) else v for v in row] for row in rows]
        clean.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), default=str))
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str).encode()
        tables[name] = {"columns": cols, "rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    print(json.dumps({"tables": tables, "digest": hashlib.sha256(json.dumps(tables, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}, sort_keys=True))
finally:
    conn.close()
"""


def installed_boards_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise board creation, routing, lifecycle and Redis domain isolation."""
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    parsed = urlsplit(redis_url)
    try:
        redis_port = parsed.port
    except ValueError as exc:
        raise ValueError("board smoke requires a valid loopback Redis URL") from exc
    if (
        parsed.scheme != "redis"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or (redis_port is not None and not 1 <= redis_port <= 65535)
        or len(parsed.path) < 2
        or not parsed.path[1:].isascii()
        or not parsed.path[1:].isdigit()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("board smoke requires an isolated loopback Redis database URL")

    aig = aig.expanduser().absolute()
    python = python.expanduser().absolute()
    cwd = cwd.expanduser().absolute()
    root = cwd / f"installed-boards-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    registry_root = root / "project"
    caller = registry_root / "nested" / "caller"
    caller.mkdir(parents=True, mode=0o700)
    evidence = root / "private-command-streams"
    evidence.mkdir(mode=0o700)
    invocations: list[dict] = []
    manifest_path = root / "manifest.jsonl"
    _exclusive(manifest_path, b"")

    child_env = dict(env)
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "AIG_ACTOR_KEY_FILE",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "AIGINEERING_API_KEY",
    ):
        child_env.pop(key, None)
    child_env["AIGINEERING_REDIS_URL"] = redis_url

    def run(
        phase: str,
        args: list[str],
        *,
        expected: int = 0,
        json_output: bool = True,
        run_env: dict[str, str] | None = None,
        timeout: int = 60,
    ):
        index = len(invocations) + 1
        argv = [str(aig), *args]
        proc = subprocess.Popen(
            argv,
            cwd=caller,
            env=run_env or child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate(timeout=10)
        stdout_path = evidence / f"{index:03d}-{phase}.stdout.raw"
        stderr_path = evidence / f"{index:03d}-{phase}.stderr.raw"
        _exclusive(stdout_path, stdout)
        _exclusive(stderr_path, stderr)
        item = {
            "phase": phase,
            "argv": argv,
            "exit": proc.returncode,
            "timed_out": timed_out,
            "stdout_file": stdout_path.name,
            "stderr_file": stderr_path.name,
            "stdout_sha256": _sha(stdout),
            "stderr_sha256": _sha(stderr),
        }
        invocations.append(item)
        with manifest_path.open("ab") as stream:
            stream.write((json.dumps(item, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if timed_out or proc.returncode != expected:
            raise RuntimeError(
                f"{phase} had unexpected exit/timeout; raw streams retained"
            )
        payload = None
        if json_output:
            try:
                payload = json.loads(stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"{phase} did not return JSON; raw streams retained"
                ) from exc
        return payload, stdout, stderr

    def run_python(phase: str, code: str, args: list[str]):
        index = len(invocations) + 1
        code_path = evidence / f"{index:03d}-{phase}.py"
        code_bytes = code.encode("utf-8")
        _exclusive(code_path, code_bytes)
        argv = [str(python), str(code_path), *args]
        proc = subprocess.Popen(
            argv,
            cwd=caller,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate(timeout=10)
        stdout_path = evidence / f"{index:03d}-{phase}.stdout.raw"
        stderr_path = evidence / f"{index:03d}-{phase}.stderr.raw"
        _exclusive(stdout_path, stdout)
        _exclusive(stderr_path, stderr)
        item = {
            "phase": phase,
            "argv": argv,
            "script_file": code_path.name,
            "script_sha256": _sha(code_bytes),
            "exit": proc.returncode,
            "timed_out": timed_out,
            "stdout_file": stdout_path.name,
            "stderr_file": stderr_path.name,
            "stdout_sha256": _sha(stdout),
            "stderr_sha256": _sha(stderr),
        }
        invocations.append(item)
        with manifest_path.open("ab") as stream:
            stream.write((json.dumps(item, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if timed_out or proc.returncode != 0:
            raise RuntimeError(f"{phase} failed; raw streams retained")
        try:
            payload = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"{phase} returned invalid JSON; raw streams retained"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{phase} returned invalid JSON shape")
        return payload, stdout, stderr

    def controlled_setup_recovery(alias: str, mode: str) -> dict:
        if mode not in {"relay", "registry-before-replace", "registry-after-replace"}:
            raise ValueError("unknown controlled board setup fault")
        fault_evidence = evidence / f"controlled-{mode}"
        fault_evidence.mkdir(mode=0o700)
        code = r"""
import hashlib, json, os, stat, sys
from pathlib import Path
from click.testing import CliRunner
import aigineering.local_boards as boards
from aigineering.application import require_redis
from aigineering.cli.main import cli
from aigineering.core.domain import load_genesis
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.runtime_services import RedisSyncPending

mode, name, registry_root, evidence_root = sys.argv[1:]
evidence = Path(evidence_root)
root = Path(registry_root).resolve()
workspace = root / ".aig" / "boards" / name
registry_path = root / ".aig" / "boards.json"

def retain(label, raw):
    path = evidence / (label + ".raw")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "file": str(path.relative_to(evidence.parent)),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }

def state():
    db = workspace / ".aig" / "store.db"
    key = workspace / ".aig" / "identity" / "root.ed25519"
    if not db.is_file() or not key.is_file():
        return {"exists": False}
    store = SQLiteStore.snapshot_from_path(db)
    try:
        manifest = load_genesis(store)
        genesis_records = store.scan_runtime_records(record_type="domain.genesis")
    finally:
        store.close()
    try:
        entry = boards.read_registry(root).by_name(name)
    except boards.BoardRegistryError:
        entry = None
    key_bytes = key.read_bytes()
    return {
        "exists": True,
        "domain_id": manifest.id,
        "genesis_count": len(genesis_records),
        "workspace": str(workspace),
        "registry_registered": entry is not None,
        "entry": None if entry is None else {
            "name": entry.name, "workspace": entry.workspace,
            "domain_id": entry.domain_id, "archived": entry.archived,
        },
        "key_sha256": hashlib.sha256(key_bytes).hexdigest(),
        "key_mode": stat.S_IMODE(key.stat().st_mode),
    }

require_redis()
fault = {"sync": False, "replaced": False, "directory_fsync": False}
original_sync = boards.sync_runtime
original_replace = os.replace
original_fsync = os.fsync

if mode == "relay":
    def fail_sync(store, *, accepted=None):
        fault["sync"] = True
        raise RedisSyncPending(accepted=None)
    boards.sync_runtime = fail_sync
elif mode == "registry-before-replace":
    def fail_replace(source, destination):
        if Path(destination) == registry_path:
            fault["replace_failed"] = True
            raise OSError("controlled registry replace failure")
        return original_replace(source, destination)
    boards.os.replace = fail_replace
elif mode == "registry-after-replace":
    def replace_then_fail_directory_sync(source, destination):
        result = original_replace(source, destination)
        if Path(destination) == registry_path:
            fault["replaced"] = True
        return result
    def fail_directory_fsync(descriptor):
        if fault["replaced"] and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            fault["directory_fsync"] = True
            raise OSError("controlled directory fsync failure")
        return original_fsync(descriptor)
    boards.os.replace = replace_then_fail_directory_sync
    boards.os.fsync = fail_directory_fsync

first_state = state()
runner = CliRunner()
first_args = ["-C", str(root), "board", "create", name, "--json"]
first = runner.invoke(cli, first_args)
first_streams = {
    "stdout": retain("injected.stdout", first.stdout.encode("utf-8")),
    "stderr": retain("injected.stderr", first.stderr.encode("utf-8")),
}
try:
    first_payload = json.loads(first.stdout)
except (UnicodeDecodeError, json.JSONDecodeError):
    first_payload = None
after_injection = state()

boards.sync_runtime = original_sync
boards.os.replace = original_replace
boards.os.fsync = original_fsync
resume = mode == "registry-after-replace"
recovery_args = ["-C", str(root), "board", "create", name]
if resume:
    recovery_args.append("--resume")
recovery_args.append("--json")
recovery = runner.invoke(cli, recovery_args)
recovery_streams = {
    "stdout": retain("recovery.stdout", recovery.stdout.encode("utf-8")),
    "stderr": retain("recovery.stderr", recovery.stderr.encode("utf-8")),
}
try:
    recovery_payload = json.loads(recovery.stdout)
except (UnicodeDecodeError, json.JSONDecodeError):
    recovery_payload = None
after_recovery = state()
print(json.dumps({
    "classification": "controlled_fault_injection",
    "mode": mode,
    "evidence_directory": str(evidence),
    "fault": fault,
    "first_exit": first.exit_code,
    "first_argv": first_args,
    "first_payload": first_payload,
    "first_streams": first_streams,
    "first_state": first_state,
    "after_injection": after_injection,
    "recovery_exit": recovery.exit_code,
    "recovery_argv": recovery_args,
    "recovery_payload": recovery_payload,
    "recovery_streams": recovery_streams,
    "after_recovery": after_recovery,
}, sort_keys=True))
"""
        result, _, _ = run_python(
            f"controlled-{mode}-and-recovery",
            code,
            [mode, names[alias], str(registry_root), str(fault_evidence)],
        )
        if result.get("classification") != "controlled_fault_injection":
            raise RuntimeError("fault control was not classified as injected")
        if result.get("first_exit") != 1:
            raise RuntimeError(f"{mode} did not retain the pending setup result")
        pending = result.get("first_payload")
        if (
            not isinstance(pending, dict)
            or pending.get("ok") is not False
            or pending.get("error") != "board_setup_pending"
            or pending.get("facts_retained") is not True
            or pending.get("phase")
            != ("redis_sync" if mode == "relay" else "registry_publish")
        ):
            raise RuntimeError(f"{mode} did not return the expected pending envelope")
        registered = mode == "registry-after-replace"
        if pending.get("registry_registered") is not registered:
            raise RuntimeError(f"{mode} reported the wrong registry durability state")
        before = result.get("first_state")
        after_fault = result.get("after_injection")
        after_recovery = result.get("after_recovery")
        if not isinstance(before, dict) or before.get("exists") is not False:
            raise RuntimeError(f"{mode} unexpectedly had a preexisting workspace")
        if (
            not isinstance(after_fault, dict)
            or after_fault.get("exists") is not True
            or after_fault.get("genesis_count") != 1
            or after_fault.get("domain_id") != pending.get("domain_id")
            or after_fault.get("registry_registered") is not registered
            or after_fault.get("key_mode") != 0o600
        ):
            raise RuntimeError(f"{mode} did not retain exactly one initialized domain")
        fault = result.get("fault")
        expected_fault = {
            "relay": fault.get("sync") is True,
            "registry-before-replace": fault.get("replace_failed") is True,
            "registry-after-replace": (
                fault.get("replaced") is True and fault.get("directory_fsync") is True
            ),
        }[mode]
        if not expected_fault:
            raise RuntimeError(f"{mode} did not reach its controlled failure point")
        recovered = result.get("recovery_payload")
        recovered_data = success_data(f"{mode} recovery", recovered)
        if (
            result.get("recovery_exit") != 0
            or recovered_data.get("name") != names[alias]
            or recovered_data.get("domain_id") != after_fault.get("domain_id")
            or not isinstance(after_recovery, dict)
            or after_recovery.get("genesis_count") != 1
            or after_recovery.get("domain_id") != after_fault.get("domain_id")
            or after_recovery.get("key_sha256") != after_fault.get("key_sha256")
            or after_recovery.get("key_mode") != 0o600
            or after_recovery.get("registry_registered") is not True
        ):
            raise RuntimeError(f"{mode} did not recover without reinitializing")
        return {
            "name": names[alias],
            "mode": mode,
            "classification": "controlled_fault_injection",
            "domain_id": after_recovery["domain_id"],
            "genesis_count": after_recovery["genesis_count"],
            "registry_registered_before_recovery": registered,
            "registry_registered_after_recovery": True,
            "key_sha256": after_recovery["key_sha256"],
        }

    def success_data(phase: str, payload: object) -> dict:
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("data"), dict)
        ):
            raise RuntimeError(f"{phase} returned an invalid success envelope")
        return payload["data"]

    origin_code = (
        "import json,pathlib,sys,aigineering; "
        "print(json.dumps({'prefix':str(pathlib.Path(sys.prefix).absolute()),"
        "'module':str(pathlib.Path(aigineering.__file__).absolute())}))"
    )

    def cli_json(phase: str, *args: str, expected: int = 0) -> dict:
        payload, _, _ = run(phase, list(args), expected=expected)
        return payload

    def fingerprint(phase: str, database: Path) -> dict:
        value, _, _ = run_python(phase, _fingerprint_program(), [str(database)])
        if not isinstance(value, dict) or not isinstance(value.get("tables"), dict):
            raise RuntimeError(f"{phase} returned invalid fingerprint shape")
        return value

    def assert_success(phase: str, payload: object) -> dict:
        data = success_data(phase, payload)
        if payload.get("ok") is not True:
            raise RuntimeError(f"{phase} was not successful")
        return data

    try:
        origin, _, _ = run_python("installed-python-origin", origin_code, [])
        prefix = Path(origin.get("prefix", "")).resolve()
        module = Path(origin.get("module", "")).resolve()
        if (
            prefix != python.parent.parent.resolve()
            or aig.parent.resolve() != prefix / "bin"
            or prefix not in module.parents
        ):
            raise RuntimeError(
                "CLI and Python do not belong to one installed environment"
            )
        _, help_stdout, _ = run("installed-cli-help", ["--help"], json_output=False)
        if b"board" not in help_stdout.lower():
            raise RuntimeError("installed CLI help does not expose board management")

        names = {
            "alpha": f"a{uuid.uuid4().hex[:10]}",
            "beta": f"b{uuid.uuid4().hex[:10]}",
            "gamma": f"g{uuid.uuid4().hex[:10]}",
            "delta": f"d{uuid.uuid4().hex[:10]}",
            "relay_pending": f"r{uuid.uuid4().hex[:10]}",
            "registry_pre_replace": f"p{uuid.uuid4().hex[:10]}",
            "registry_post_replace": f"q{uuid.uuid4().hex[:10]}",
            "adopted": f"x{uuid.uuid4().hex[:10]}",
        }

        created = {}
        for alias in ("alpha", "beta"):
            payload, create_stdout, create_stderr = run(
                f"create-{alias}",
                ["-C", str(registry_root), "board", "create", names[alias], "--json"],
            )
            row = assert_success(f"create-{alias}", payload)
            if (
                row.get("name") != names[alias]
                or row.get("availability") != "available"
            ):
                raise RuntimeError(f"create-{alias} returned an unexpected board entry")
            workspace = Path(row.get("workspace", ""))
            if not workspace.is_absolute() or not workspace.is_dir():
                raise RuntimeError(
                    f"create-{alias} did not create an absolute workspace"
                )
            domain_id = row.get("domain_id")
            if not isinstance(domain_id, str) or not domain_id:
                raise RuntimeError(f"create-{alias} omitted its Genesis ID")
            key = workspace / ".aig" / "identity" / "root.ed25519"
            key_bytes = key.read_bytes()
            if key.stat().st_mode & 0o777 != 0o600 or not key_bytes:
                raise RuntimeError(f"create-{alias} root key is missing or not private")
            if key_bytes in create_stdout or key_bytes in create_stderr:
                raise RuntimeError(f"create-{alias} exposed private root key bytes")
            created[alias] = {
                "name": names[alias],
                "workspace": workspace,
                "domain_id": domain_id,
                "entry": row,
                "key": key,
                "key_sha256": _sha(key_bytes),
                "key_bytes": key_bytes,
            }
        if created["alpha"]["domain_id"] == created["beta"]["domain_id"]:
            raise RuntimeError("managed boards shared one Genesis domain")
        if created["alpha"]["key_bytes"] == created["beta"]["key_bytes"]:
            raise RuntimeError("managed boards shared one private root key")

        registry_file = registry_root / ".aig" / "boards.json"
        registry_before_adoption = registry_file.read_bytes()
        adopted_workspace = root / "adopted-external-workspace"
        adopted_workspace.mkdir(mode=0o700)
        initialized, init_stdout, init_stderr = run(
            "initialize-external-adoption-workspace",
            [
                "-C",
                str(adopted_workspace),
                "domain",
                "init",
                "--domain",
                f"external-fixture-{uuid.uuid4().hex[:10]}",
                "--json",
            ],
        )
        adopted_domain_id = initialized.get("domain_id")
        key_file = Path(initialized.get("key_file", ""))
        if (
            not isinstance(adopted_domain_id, str)
            or not adopted_domain_id
            or not key_file.is_file()
            or key_file.stat().st_mode & 0o777 != 0o600
        ):
            raise RuntimeError("external domain init omitted its pinned identity")
        adopted_key_before = key_file.read_bytes()
        if adopted_key_before in init_stdout or adopted_key_before in init_stderr:
            raise RuntimeError("domain initialization exposed private key bytes")
        adoption_workspace_db = adopted_workspace / ".aig" / "store.db"
        adoption_before = fingerprint(
            "external-workspace-before-adoption", adoption_workspace_db
        )
        adoption_entry = assert_success(
            "adopt-existing-external-workspace",
            cli_json(
                "adopt-existing-external-workspace",
                "-C",
                str(registry_root),
                "board",
                "create",
                names["adopted"],
                "--workspace",
                str(adopted_workspace),
                "--json",
            ),
        )
        if (
            adoption_entry.get("name") != names["adopted"]
            or adoption_entry.get("workspace") != str(adopted_workspace.resolve())
            or adoption_entry.get("domain_id") != adopted_domain_id
            or adoption_entry.get("availability") != "available"
            or key_file.read_bytes() != adopted_key_before
        ):
            raise RuntimeError("workspace adoption changed its existing identity")
        registry_after_adoption = registry_file.read_bytes()
        if json.loads(registry_before_adoption).get("selected") != json.loads(
            registry_after_adoption
        ).get("selected"):
            raise RuntimeError("workspace adoption changed the selected board")
        adoption_resume = assert_success(
            "resume-adopted-external-workspace",
            cli_json(
                "resume-adopted-external-workspace",
                "-C",
                str(registry_root),
                "board",
                "create",
                names["adopted"],
                "--workspace",
                str(adopted_workspace),
                "--resume",
                "--json",
            ),
        )
        if (
            adoption_resume != adoption_entry
            or registry_file.read_bytes() != registry_after_adoption
            or key_file.read_bytes() != adopted_key_before
        ):
            raise RuntimeError(
                "adopted workspace resume changed its identity or registry"
            )
        routed_domain = cli_json(
            "route-to-adopted-workspace",
            "--board",
            names["adopted"],
            "domain",
            "show",
            "--json",
        )
        if routed_domain.get("domain_id") != adopted_domain_id:
            raise RuntimeError("--board did not route to the adopted workspace")
        adoption_before_duplicate = fingerprint(
            "external-workspace-before-duplicate-adoption", adoption_workspace_db
        )
        duplicate, _, _ = run(
            "duplicate-adoption-alias-rejected",
            [
                "-C",
                str(registry_root),
                "board",
                "create",
                names["adopted"],
                "--workspace",
                str(adopted_workspace),
                "--json",
            ],
            expected=1,
        )
        if duplicate.get("ok") is not False:
            raise RuntimeError("duplicate adoption alias was not rejected")
        adoption_after_duplicate = fingerprint(
            "external-workspace-after-duplicate-adoption", adoption_workspace_db
        )
        if adoption_before_duplicate != adoption_after_duplicate:
            raise RuntimeError(
                "duplicate adoption changed the external workspace Store"
            )
        if adoption_before != adoption_before_duplicate:
            raise RuntimeError("adoption changed SQLite facts or materializations")

        listed = assert_success(
            "initial-board-list",
            cli_json("initial-board-list", "board", "list", "--json"),
        )
        rows = listed.get("boards")
        expected_initial_names = {
            names["alpha"],
            names["beta"],
            names["adopted"],
        }
        if not isinstance(rows, list) or {row.get("name") for row in rows} != (
            expected_initial_names
        ):
            raise RuntimeError("board list did not report both newly created boards")
        if listed.get("selected") is not None:
            raise RuntimeError("board creation unexpectedly changed selection")
        registry_file = registry_root / ".aig" / "boards.json"
        if not registry_file.is_file() or registry_file.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("board registry is missing or not private")
        registry_before_resume = registry_file.read_bytes()
        alpha_key_before_resume = created["alpha"]["key"].read_bytes()
        resumed = cli_json(
            "resume-existing-alpha",
            "board",
            "create",
            names["alpha"],
            "--resume",
            "--json",
        )
        resumed_data = assert_success("resume-existing-alpha", resumed)
        if resumed_data != created["alpha"]["entry"]:
            raise RuntimeError("resume did not report the exact existing board entry")
        if (
            registry_file.read_bytes() != registry_before_resume
            or created["alpha"]["key"].read_bytes() != alpha_key_before_resume
            or created["alpha"]["key"].stat().st_mode & 0o777 != 0o600
            or created["alpha"]["domain_id"] != resumed_data.get("domain_id")
        ):
            raise RuntimeError(
                "resume changed existing board identity or registry state"
            )

        controlled_failures = [
            controlled_setup_recovery("relay_pending", "relay"),
            controlled_setup_recovery(
                "registry_pre_replace", "registry-before-replace"
            ),
            controlled_setup_recovery(
                "registry_post_replace", "registry-after-replace"
            ),
        ]

        task_rows: dict[str, dict] = {}
        asset_ids: dict[str, str] = {}
        asset_contents = {
            "alpha": f"BOARD_ALPHA_{uuid.uuid4().hex}",
            "beta": f"BOARD_BETA_{uuid.uuid4().hex}",
        }
        for alias in ("alpha", "beta"):
            selected = assert_success(
                f"switch-{alias}",
                cli_json(f"switch-{alias}", "board", "switch", names[alias], "--json"),
            )
            if selected.get("name") != names[alias]:
                raise RuntimeError(f"switch-{alias} did not select requested board")
            task_payload = cli_json(
                f"task-create-{alias}",
                "task",
                "create",
                "--name",
                "shared-board-task",
                "--description",
                f"private description {alias}",
                "--output",
                "shared-result",
                "--json",
            )
            if (
                task_payload.get("ok") is not True
                or task_payload.get("name") != "shared-board-task"
                or task_payload.get("outputs") != ["shared-result"]
            ):
                raise RuntimeError(f"task-create-{alias} result was unexpected")
            task_id = task_payload.get("contract_id")
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError(f"task-create-{alias} omitted contract_id")
            task_rows[alias] = {
                "contract_id": task_id,
                "description": f"private description {alias}",
            }
            asset_payload = cli_json(
                f"asset-add-{alias}",
                "asset",
                "add",
                "--name",
                "shared-board-asset",
                "--content",
                asset_contents[alias],
                "--json",
            )
            if (
                not isinstance(asset_payload, dict)
                or asset_payload.get("name") != "shared-board-asset"
            ):
                raise RuntimeError(f"asset-add-{alias} result was unexpected")
            asset_id = asset_payload.get("id")
            if not isinstance(asset_id, str) or not asset_id:
                raise RuntimeError(f"asset-add-{alias} omitted its Asset ID")
            asset_ids[alias] = asset_id

        if asset_ids["alpha"] == asset_ids["beta"]:
            raise RuntimeError("different same-name assets unexpectedly shared an ID")

        # A real in-process invocation checks -C cwd restoration; a child CLI
        # process cannot observe whether its parent's cwd was restored.
        cwd_probe = r"""
import json, sys
from pathlib import Path
from click.testing import CliRunner
from aigineering.cli.main import cli
before = str(Path.cwd().resolve())
result = CliRunner().invoke(cli, ["-C", sys.argv[1], "domain", "show", "--json"])
after = str(Path.cwd().resolve())
print(json.dumps({"before": before, "after": after, "exit": result.exit_code, "output": result.output}))
"""
        cwd_check, _, _ = run_python(
            "cwd-restore-cli-invocation",
            cwd_probe,
            [str(created["beta"]["workspace"])],
        )
        if cwd_check.get("exit") != 0 or cwd_check.get("before") != cwd_check.get(
            "after"
        ):
            raise RuntimeError("installed CLI did not restore cwd after explicit -C")
        try:
            cwd_output = json.loads(cwd_check["output"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("-C domain show did not return JSON") from exc
        if cwd_output.get("domain_id") != created["beta"]["domain_id"]:
            raise RuntimeError("-C cwd probe entered the wrong domain")

        # A fresh process must route to the persistent selection, then an
        # explicit switch establishes alpha for the override checks.
        beta_tasks = cli_json("default-beta-task-list", "task", "list", "--json")
        if not any(
            row.get("contract_id") == task_rows["beta"]["contract_id"]
            for row in beta_tasks.get("data", {}).get("tasks", [])
        ):
            raise RuntimeError("fresh default did not route to selected beta")
        alpha_selected = assert_success(
            "switch-alpha-for-routing-controls",
            cli_json(
                "switch-alpha-for-routing-controls",
                "board",
                "switch",
                names["alpha"],
                "--json",
            ),
        )
        if alpha_selected.get("name") != names["alpha"]:
            raise RuntimeError("could not select alpha for routing controls")

        alpha_output_payload = cli_json(
            "alpha-only-declared-output-asset",
            "--board",
            names["alpha"],
            "asset",
            "add",
            "--name",
            "shared-result",
            "--content",
            f"ALPHA_ONLY_OUTPUT_{uuid.uuid4().hex}",
            "--json",
        )
        alpha_output_asset_id = alpha_output_payload.get("id")
        if not isinstance(alpha_output_asset_id, str) or not alpha_output_asset_id:
            raise RuntimeError("alpha-only output Asset omitted its ID")

        # A new process must honor persistent selection, while --board is only
        # a one-command override and explicit -C bypasses registry selection.
        asset_list = cli_json("default-alpha-assets", "asset", "ls", "--json")
        if not isinstance(asset_list, list) or not any(
            item.get("id") == asset_ids["alpha"] for item in asset_list
        ):
            raise RuntimeError("a fresh process did not route to selected alpha")
        if any(item.get("id") == asset_ids["beta"] for item in asset_list):
            raise RuntimeError("beta Asset leaked into alpha projection")
        selected_beta = cli_json(
            "temporary-beta-assets", "--board", names["beta"], "asset", "ls", "--json"
        )
        if not isinstance(selected_beta, list) or not any(
            item.get("id") == asset_ids["beta"] for item in selected_beta
        ):
            raise RuntimeError("--board did not route one command to beta")
        default_again = cli_json("default-stays-alpha", "asset", "ls", "--json")
        if not isinstance(default_again, list) or any(
            item.get("id") == asset_ids["beta"] for item in default_again
        ):
            raise RuntimeError("temporary --board changed persistent selection")
        explicit_beta = cli_json(
            "explicit-c-bypasses-selection",
            "-C",
            str(created["beta"]["workspace"]),
            "asset",
            "ls",
            "--json",
        )
        if not isinstance(explicit_beta, list) or not any(
            item.get("id") == asset_ids["beta"] for item in explicit_beta
        ):
            raise RuntimeError("explicit -C did not bypass the selected alpha board")
        _, _, conflict_stderr = run(
            "board-and-c-conflict",
            [
                "--board",
                names["beta"],
                "-C",
                str(created["beta"]["workspace"]),
                "asset",
                "ls",
                "--json",
            ],
            expected=2,
            json_output=False,
        )
        # The usage error is intentionally not JSON: click rejects the global
        # option pair before dispatching the command.
        if b"mutually exclusive" not in conflict_stderr:
            raise RuntimeError("--board/-C conflict did not report its usage rule")

        for alias in ("alpha", "beta"):
            exact = cli_json(
                f"exact-asset-{alias}",
                "--board",
                names[alias],
                "asset",
                "show",
                "--id",
                asset_ids[alias],
                "--json",
            )
            if (
                exact.get("id") != asset_ids[alias]
                or exact.get("content") != asset_contents[alias]
            ):
                raise RuntimeError(f"exact Asset query failed for {alias}")
            status = cli_json(
                f"task-status-{alias}",
                "--board",
                names[alias],
                "task",
                "status",
                task_rows[alias]["contract_id"],
                "--json",
            )
            if (
                status.get("contract_id") != task_rows[alias]["contract_id"]
                or status.get("name") != "shared-board-task"
            ):
                raise RuntimeError(f"task status lookup failed for {alias}")
            asserted_outputs = status.get("outputs")
            if not isinstance(asserted_outputs, dict):
                raise RuntimeError(f"task status omitted output map for {alias}")
            if alias == "alpha":
                if (
                    asserted_outputs.get("shared-result") != alpha_output_asset_id
                    or status.get("outputs_satisfied") is not True
                    or status.get("status") != "completed"
                ):
                    raise RuntimeError(
                        "alpha task did not project its same-domain declared output"
                    )
            elif (
                "shared-result" in asserted_outputs
                or status.get("outputs_satisfied") is not False
                or status.get("status") == "completed"
            ):
                raise RuntimeError("alpha output Asset leaked into beta task status")
            foreign = "beta" if alias == "alpha" else "alpha"
            if alias == "alpha":
                foreign_task, _, _ = run(
                    "foreign-task-denied-alpha",
                    [
                        "--board",
                        names[alias],
                        "task",
                        "status",
                        task_rows[foreign]["contract_id"],
                        "--json",
                    ],
                    expected=1,
                )
                if (
                    not isinstance(foreign_task, dict)
                    or foreign_task.get("ok") is not False
                    or "not found" not in str(foreign_task.get("error", "")).lower()
                ):
                    raise RuntimeError(
                        "foreign task did not return its safe not-found result"
                    )
            run(
                f"foreign-asset-denied-{alias}",
                [
                    "--board",
                    names[alias],
                    "asset",
                    "show",
                    "--id",
                    asset_ids[foreign],
                    "--json",
                ],
                expected=1,
                json_output=False,
            )
            if alias == "alpha":
                # Verify the safe not-found path without depending on human text.
                foreign_stderr = (
                    evidence
                    / f"{len(invocations):03d}-foreign-asset-denied-alpha.stderr.raw"
                ).read_bytes()
                if not foreign_stderr:
                    raise RuntimeError("foreign Asset denial omitted its diagnostic")

        # Archive/restore is only registry metadata: compare the full SQLite
        # table map, including sqlite_sequence, around both transitions.
        alpha_db = created["alpha"]["workspace"] / ".aig" / "store.db"
        before_archive = fingerprint("alpha-before-archive", alpha_db)
        archive_data = assert_success(
            "archive-alpha",
            cli_json("archive-alpha", "board", "archive", names["alpha"], "--json"),
        )
        if archive_data.get("archived") is not True:
            raise RuntimeError("archive command did not mark alpha archived")
        _, _, archived_stderr = run(
            "archived-default-blocked",
            ["task", "list", "--json"],
            expected=1,
            json_output=False,
        )
        if b"archived" not in archived_stderr.lower():
            raise RuntimeError("archived selected board did not report a blocker")
        restore_data = assert_success(
            "restore-alpha",
            cli_json(
                "restore-alpha",
                "board",
                "archive",
                names["alpha"],
                "--restore",
                "--json",
            ),
        )
        if restore_data.get("archived") is not False:
            raise RuntimeError("restore command did not clear archived state")
        after_archive = fingerprint("alpha-after-archive-restore", alpha_db)
        if before_archive != after_archive:
            raise RuntimeError(
                "archive/restore changed SQLite facts or materializations"
            )

        # Rebuild each domain separately, then recheck exact IDs and no leakage.
        for alias in ("alpha", "beta"):
            before_rebuild = fingerprint(
                f"{alias}-before-redis-rebuild",
                created[alias]["workspace"] / ".aig" / "store.db",
            )
            rebuild = cli_json(
                f"redis-rebuild-{alias}",
                "-C",
                str(created[alias]["workspace"]),
                "redis",
                "rebuild",
                "--json",
            )
            if rebuild.get("ok") is not True or rebuild.get("current") is not True:
                raise RuntimeError(f"Redis rebuild did not report current for {alias}")
            board_assets = cli_json(
                f"assets-after-rebuild-{alias}",
                "-C",
                str(created[alias]["workspace"]),
                "asset",
                "ls",
                "--json",
            )
            expected_id = asset_ids[alias]
            foreign_id = asset_ids["beta" if alias == "alpha" else "alpha"]
            if not any(item.get("id") == expected_id for item in board_assets) or any(
                item.get("id") == foreign_id for item in board_assets
            ):
                raise RuntimeError(
                    f"domain isolation failed after Redis rebuild for {alias}"
                )
            after_rebuild = fingerprint(
                f"{alias}-after-redis-rebuild",
                created[alias]["workspace"] / ".aig" / "store.db",
            )
            if before_rebuild != after_rebuild:
                raise RuntimeError(f"Redis rebuild changed SQLite facts for {alias}")

        # Exact pin replacement uses disposable database copies only. Keep a
        # recoverable original and restore it even when the oracle fails.
        beta_db = created["beta"]["workspace"] / ".aig" / "store.db"
        before_pin_probe = fingerprint("alpha-before-pin-replacement", alpha_db)
        pin_backup = root / "alpha-store-original.db"
        replacement = root / "alpha-store-replacement.db"
        restore_script = r"""
import sqlite3, sys
source, target = sys.argv[1:3]
from pathlib import Path
src = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
Path(target).chmod(0o600)
print('{"ok":true}')
"""

        def backup_database(phase: str, source: Path, target: Path) -> None:
            payload, _, _ = run_python(
                phase, restore_script, [str(source), str(target)]
            )
            if payload != {"ok": True}:
                raise RuntimeError(f"{phase} returned unexpected backup metadata")

        backup_database("backup-alpha-before-pin-probe", alpha_db, pin_backup)
        backup_database("copy-beta-for-pin-probe", beta_db, replacement)
        alpha_db.unlink()
        for sidecar in (Path(str(alpha_db) + "-wal"), Path(str(alpha_db) + "-shm")):
            sidecar.unlink(missing_ok=True)
        os.replace(replacement, alpha_db)
        try:
            _, _, pin_stderr = run(
                "replacement-domain-pin-rejected",
                ["domain", "show", "--json"],
                expected=1,
                json_output=False,
            )
            if (
                b"match" not in pin_stderr.lower()
                and b"domain" not in pin_stderr.lower()
            ):
                raise RuntimeError(
                    "replacement Genesis pin rejection omitted diagnostics"
                )
        finally:
            alpha_db.unlink(missing_ok=True)
            for sidecar in (Path(str(alpha_db) + "-wal"), Path(str(alpha_db) + "-shm")):
                sidecar.unlink(missing_ok=True)
            os.replace(pin_backup, alpha_db)
        repaired = cli_json(
            "original-pin-restored",
            "-C",
            str(created["alpha"]["workspace"]),
            "domain",
            "show",
            "--json",
        )
        if repaired.get("domain_id") != created["alpha"]["domain_id"]:
            raise RuntimeError("restored alpha Store did not match its pinned Genesis")
        after_pin_probe = fingerprint("alpha-after-pin-restoration", alpha_db)
        if before_pin_probe != after_pin_probe:
            raise RuntimeError("pin replacement probe changed alpha SQLite contents")

        # Two real processes contend on one registry lock with distinct aliases.
        concurrent = {}
        processes = []
        for alias in ("gamma", "delta"):
            argv = [str(aig), "board", "create", names[alias], "--json"]
            proc = subprocess.Popen(
                argv,
                cwd=caller,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            processes.append((alias, argv, proc))
        for alias, argv, proc in processes:
            try:
                stdout, stderr = proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = proc.communicate(timeout=10)
                timed_out = True
            else:
                timed_out = False
            idx = len(invocations) + 1
            out_path = evidence / f"{idx:03d}-concurrent-create-{alias}.stdout.raw"
            err_path = evidence / f"{idx:03d}-concurrent-create-{alias}.stderr.raw"
            _exclusive(out_path, stdout)
            _exclusive(err_path, stderr)
            record = {
                "phase": f"concurrent-create-{alias}",
                "argv": argv,
                "exit": proc.returncode,
                "timed_out": timed_out,
                "stdout_file": out_path.name,
                "stderr_file": err_path.name,
                "stdout_sha256": _sha(stdout),
                "stderr_sha256": _sha(stderr),
            }
            invocations.append(record)
            with manifest_path.open("ab") as stream:
                stream.write((json.dumps(record, sort_keys=True) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            concurrent[alias] = {
                "exit": proc.returncode,
                "timed_out": timed_out,
                "stdout": stdout,
            }
        failed_concurrent = [
            alias
            for alias, result in concurrent.items()
            if result["timed_out"] or result["exit"] != 0
        ]
        if failed_concurrent:
            raise RuntimeError(
                "concurrent create failed for " + ", ".join(failed_concurrent)
            )
        for alias, result in list(concurrent.items()):
            try:
                value = json.loads(result["stdout"])
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"concurrent create {alias} returned invalid JSON"
                ) from exc
            concurrent[alias] = success_data(f"concurrent create {alias}", value)
        if (
            concurrent["gamma"].get("name") != names["gamma"]
            or concurrent["delta"].get("name") != names["delta"]
        ):
            raise RuntimeError("concurrent create result names do not match requests")
        final_list = assert_success(
            "final-board-list", cli_json("final-board-list", "board", "list", "--json")
        )
        final_names = {row.get("name") for row in final_list.get("boards", [])}
        if final_names != set(names.values()):
            raise RuntimeError("concurrent registry update lost a board entry")
        final_domains = {row.get("domain_id") for row in final_list["boards"]}
        final_paths = {row.get("workspace") for row in final_list["boards"]}
        if len(final_domains) != len(final_list["boards"]) or len(final_paths) != len(
            final_list["boards"]
        ):
            raise RuntimeError("registry contains duplicate board domains or paths")

        summary = {
            "schema": "aig-installed-board-smoke-v1",
            "status": "passed",
            "board_names": names,
            "domain_ids": {name: item["domain_id"] for name, item in created.items()},
            "asset_ids": asset_ids,
            "alpha_only_output_asset_id": alpha_output_asset_id,
            "task_ids": {name: item["contract_id"] for name, item in task_rows.items()},
            "controlled_setup_recoveries": controlled_failures,
            "adopted_workspace": {
                "name": names["adopted"],
                "workspace": str(adopted_workspace),
                "domain_id": adopted_domain_id,
                "key_sha256": _sha(adopted_key_before),
                "registry_registered": True,
                "resumed_exact_entry": True,
                "duplicate_alias_rejected": True,
            },
            "invocations": len(invocations),
            "provider_calls": 0,
            "worker_calls": 0,
            "limitations": [
                "No provider or Worker execution was performed.",
                "Injected relay and registry-write failures were controlled test seams, not real outages.",
                "No real Redis outage or physical disk failure was exercised.",
                "SQLite domain replacement was an isolated disposable fixture.",
            ],
        }
        summary_path = root / "summary.json"
        _exclusive(
            summary_path,
            (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode(),
        )
        return {"root": str(root), **summary}
    except BaseException as exc:
        failure_path = root / "failure.json"
        payload = {
            "schema": "aig-installed-board-smoke-v1",
            "status": "failed",
            "phase": invocations[-1]["phase"] if invocations else "setup",
            "error_type": type(exc).__name__,
            "invocations": len(invocations),
        }
        try:
            _exclusive(
                failure_path,
                (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode(),
            )
        except FileExistsError:
            pass
        raise


__all__ = ["installed_boards_smoke"]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aig", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--redis-url", required=True)
    args = parser.parse_args()
    child_env = dict(os.environ)
    child_env["AIGINEERING_REDIS_URL"] = args.redis_url
    result = installed_boards_smoke(
        args.aig, args.python, cwd=args.evidence_dir, env=child_env
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
