"""Source-external installed controls for immutable reassignment requests.

The root acceptance harness supplies a fresh scratch parent and the installed
Python interpreter. This helper uses ordinary signed CLI operations, with one
fixture-backed process stop at the real request-save seam. It does not invoke a
Worker or provider. Raw streams and Store evidence stay in a private directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import uuid


_PENDING_HELPER_SHA256 = (
    "665d4d6dbe8bf57ae941cc26a777441a8411aee6212049b31223ca8d02482a4f"
)
_REASSIGN_HELPER_SHA256 = (
    "92312129fae46371989d2fab5e8db10f945bfa0c5c168238ce0f37e7bf7032da"
)
_TABLES = {
    "schema_version",
    "assets",
    "contracts",
    "trace_events",
    "sessions",
    "claims",
    "replacement_claims",
    "worker_claims",
    "idempotency_records",
    "contract_activation_refs",
    "runtime_lifecycle",
    "contract_declared_outputs",
    "worker_registrations",
    "runtime_records",
    "asset_contents",
    "asset_definitions",
    "asset_definition_content_assertions",
    "sqlite_sequence",
}
_PROVIDER_KEYS = {
    "AIGINEERING_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_OPENAI_AD_TOKEN",
}
_REQUEST_FIELDS = {
    "schema",
    "store_path",
    "domain_id",
    "observed_revision",
    "targets",
    "previous_by_target",
    "action",
    "worker_id",
    "reason",
    "batch_base",
    "request_id",
}

_INSTALL_ORIGIN = r"""
import json,pathlib,sys,aigineering
print(json.dumps({'prefix':str(pathlib.Path(sys.prefix).absolute()),'module':str(pathlib.Path(aigineering.__file__).absolute()),'executable':str(pathlib.Path(sys.executable).absolute())}))
"""

_WORKER_PUBLIC_KEY = r"""
import json
from aigineering.core.signing import Ed25519Signer
signer=Ed25519Signer()
print(json.dumps({'public_key':signer.signer_id,'signature_kind':signer.kind}))
"""

_READ_REQUEST = r"""
import json,os,stat,sys
from pathlib import Path
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.identity import canonical_json
from aigineering.task_assignment_requests import load_assignment_request,validate_assignment_request_domain
store_path,request_id=sys.argv[1:]
resolved=Path(store_path).resolve(strict=True)
directory=resolved.parent/'requests'/'reassign'
target=directory/f'request-{request_id}.json'
if target.is_symlink(): raise RuntimeError('request path is a symlink')
fd=os.open(target,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
try:
 info=os.fstat(fd)
 if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>1024*1024: raise RuntimeError('request file metadata differs')
 chunks=bytearray()
 while len(chunks)<=1024*1024:
  block=os.read(fd,min(65536,1024*1024+1-len(chunks)))
  if not block: break
  chunks.extend(block)
 raw=bytes(chunks)
finally: os.close(fd)
request=load_assignment_request(resolved,request_id)
if canonical_json(request).encode('utf-8')!=raw: raise RuntimeError('request bytes differ from canonical loader result')
snapshot=SQLiteStore.snapshot_from_path(resolved)
try: validate_assignment_request_domain(request,snapshot)
finally: snapshot.close()
print(json.dumps({'request':request,'file_sha256':__import__('hashlib').sha256(raw).hexdigest(),'file_bytes':len(raw),'mode':stat.S_IMODE(target.stat().st_mode),'directory_mode':stat.S_IMODE(directory.stat().st_mode)},sort_keys=True))
"""

_INTERRUPT_AFTER_SAVE = r"""
import os,sys
from aigineering.cli.main import cli
import aigineering.cli.task_assignment as task_cli
real_save=task_cli.save_assignment_request
def save_then_exit(*args,**kwargs):
 real_save(*args,**kwargs)
 os._exit(86)
task_cli.save_assignment_request=save_then_exit
cli.main(args=sys.argv[1:],prog_name='aig',standalone_mode=True)
"""

_REQUEST_ENUM = r"""
import json,sys
from pathlib import Path
store=Path(sys.argv[1]).resolve(strict=True)
directory=store.parent/'requests'/'reassign'
paths=sorted(directory.glob('request-*.json')) if directory.exists() else []
print(json.dumps({'names':[p.name for p in paths]},sort_keys=True))
"""

_REHASH_REQUEST = r"""
import hashlib,json,sys
from pathlib import Path
from aigineering.protocol.identity import canonical_json
source,destination_store,directory=sys.argv[1:]
request=json.loads(Path(source).read_text(encoding='utf-8'))
request['store_path']=str(Path(destination_store).resolve(strict=True))
request.pop('request_id',None)
request_id=hashlib.sha256(canonical_json(request).encode('utf-8')).hexdigest()
request['request_id']=request_id
path=Path(directory)/f'request-{request_id}.json'
path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
payload=canonical_json(request).encode('utf-8')
fd=__import__('os').open(path,__import__('os').O_WRONLY|__import__('os').O_CREAT|__import__('os').O_EXCL,0o600)
with __import__('os').fdopen(fd,'wb') as f:f.write(payload);f.flush();__import__('os').fsync(f.fileno())
print(json.dumps({'request_id':request_id,'bytes':len(payload)},sort_keys=True))
"""


def _write_exclusive(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_request(request: dict) -> str:
    return json.dumps(
        request, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _json_error_code(helper, payload: object) -> str | None:
    return helper._json_error_code(payload)


def _load_pinned_helpers():
    root = Path(__file__).parent
    pending_path = root / "installed_assignment_priority_pending_smoke.py"
    reassignment_path = root / "installed_bulk_reassignment_smoke.py"
    if _sha256(pending_path.read_bytes()) != _PENDING_HELPER_SHA256:
        raise RuntimeError("pinned assignment relay helper bytes changed")
    if _sha256(reassignment_path.read_bytes()) != _REASSIGN_HELPER_SHA256:
        raise RuntimeError("pinned reassignment proof helper bytes changed")

    def load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("pinned helper could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    pending = load("_assignment_pending_pinned", pending_path)
    reassignment = load("_bulk_reassignment_pinned", reassignment_path)
    for name in ("_FINGERPRINT", "_PROVE_FACT", "_FEED_PROOF", "_relay_proxy"):
        if not hasattr(pending, name):
            raise RuntimeError("pinned relay helper API is incomplete")
    for name in ("_READ_ASSIGNMENTS", "_REBUILD", "_assert_pending_projection"):
        if not hasattr(reassignment, name):
            raise RuntimeError("pinned reassignment helper API is incomplete")
    return pending, reassignment


def _local_redis(value: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    db = parsed.path.lstrip("/")
    if (
        parsed.scheme != "redis"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or db != "12"
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("helper requires explicit local Redis database 12")
    return value


def _validate_origin(python: Path, aig: Path, origin: dict) -> dict:
    prefix = Path(origin.get("prefix", "")).resolve()
    module = Path(origin.get("module", "")).resolve()
    reported_executable_lexical = Path(origin.get("executable", "")).absolute()
    reported_executable = reported_executable_lexical.resolve()
    python_lexical = python.absolute()
    aig_lexical = aig.absolute()
    python_real = python_lexical.resolve()
    aig_real = aig_lexical.resolve()
    bin_directory = prefix / "bin"
    if (
        not python_lexical.is_file()
        or not aig_lexical.is_file()
        or reported_executable != python_real
        or python_lexical.parent.resolve() != bin_directory
        or reported_executable_lexical.parent.resolve() != bin_directory
        or aig_lexical.parent.resolve() != bin_directory
        or aig_real.parent != bin_directory
        or not module.is_relative_to(prefix)
        or "site-packages" not in module.parts
    ):
        raise RuntimeError("installed Python, CLI, and package origin do not match")
    return {
        "prefix": str(prefix),
        "bin_directory": str(bin_directory),
        "module": str(module),
        "python": str(python_lexical),
        "python_realpath": str(python_real),
        "aig": str(aig_lexical),
        "aig_realpath": str(aig_real),
        "scope": "same venv/site-packages; caller separately checks source checkout exclusion",
    }


def _request_file(store_path: Path, request_id: str) -> Path:
    return store_path.parent / "requests" / "reassign" / f"request-{request_id}.json"


def _assert_request(
    request: object,
    *,
    request_id: str,
    store_path: Path,
    domain_id: str,
    targets: tuple[str, ...],
    previous: dict[str, str],
    action: str,
    worker_id: str | None,
    reason: str,
    batch_base: str,
    observed_revision: int,
) -> dict:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise RuntimeError("saved request has an unexpected schema")
    if (
        request.get("schema") != "aig-task-reassignment-request-v1"
        or request.get("request_id") != request_id
        or request_id != _independent_request_id(request)
        or request.get("store_path") != str(store_path.resolve())
        or request.get("domain_id") != domain_id
        or type(request.get("observed_revision")) is not int
        or request.get("observed_revision") != observed_revision
        or request.get("targets") != list(targets)
        or request.get("previous_by_target") != previous
        or request.get("action") != action
        or request.get("worker_id") != worker_id
        or request.get("reason") != reason
        or request.get("batch_base") != batch_base
    ):
        raise RuntimeError("saved request does not bind the exact frozen intent")
    return request


def _independent_request_id(request: dict) -> str:
    body = {key: value for key, value in request.items() if key != "request_id"}
    encoded = json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _independent_target_key(
    batch_base: str, contract_id: str, previous_assignment_id: str
) -> str:
    intent = (
        "task-bulk-v1",
        "reassign",
        batch_base,
        contract_id,
        previous_assignment_id,
    )
    encoded = json.dumps(
        intent, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8", errors="strict")
    return f"task-bulk-v1:reassign:{hashlib.sha256(encoded).hexdigest()}"


def _assert_result(
    payload: object,
    *,
    request_id: str,
    targets: tuple[str, ...],
    previous: dict[str, str],
    worker_id: str,
    reason: str,
    expected_batch_base: str,
) -> tuple[dict, list[dict]]:
    if (
        re.fullmatch(r"[0-9a-f]{64}", request_id) is None
        or not isinstance(expected_batch_base, str)
        or not expected_batch_base
        or len(targets) == 0
        or len(set(targets)) != len(targets)
        or set(previous) != set(targets)
    ):
        raise RuntimeError("result oracle inputs are not a complete frozen request")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"ok", "data", "revision"}
        or type(payload.get("revision")) is not int
        or not isinstance(payload.get("data"), dict)
    ):
        raise RuntimeError("current/resume result envelope is malformed")
    data = payload["data"]
    rows = data.get("results")
    if (
        data.get("request_id") != request_id
        or data.get("batch_idempotency_key") != expected_batch_base
        or not isinstance(rows, list)
        or len(rows) != len(targets)
        or any(not isinstance(row, dict) for row in rows)
        or [row.get("contract_id") for row in rows] != list(targets)
    ):
        raise RuntimeError("request result rows are incomplete or out of order")
    for row in rows:
        contract_id = row["contract_id"]
        expected_key = _independent_target_key(
            expected_batch_base, contract_id, previous[contract_id]
        )
        if (
            row.get("request_id") != request_id
            or row.get("requested_previous_assignment_id") != previous[contract_id]
            or row.get("idempotency_key") != expected_key
        ):
            raise RuntimeError(
                "result row lost its request/head context or has the wrong target key"
            )
        row_data = row.get("data")
        if row_data is not None and not isinstance(row_data, dict):
            raise RuntimeError("result row data is not an object")
        if (
            isinstance(row_data, dict)
            and row_data.get("idempotency_key") != expected_key
        ):
            raise RuntimeError("result data has the wrong target idempotency key")
        outcome = row.get("outcome")
        if outcome in {"accepted", "redis_sync_pending"}:
            operation = row.get("data")
            if (
                not isinstance(operation, dict)
                or operation.get("contract_id") != contract_id
                or operation.get("previous_assignment_id") != previous[contract_id]
                or operation.get("worker_id") != worker_id
                or operation.get("reason") != reason
                or operation.get("record_type") != "claim.assignment.changed"
                or not isinstance(operation.get("record_id"), str)
                or not operation.get("record_id")
                or not isinstance(operation.get("candidate_id"), str)
                or not operation.get("candidate_id")
                or not isinstance(operation.get("idempotency_key"), str)
                or not operation.get("idempotency_key")
                or operation.get("idempotency_key") != expected_key
                or row.get("candidate_id") != operation.get("candidate_id")
                or row.get("idempotency_key") != operation.get("idempotency_key")
                or row.get("fact_ids") != [operation.get("record_id")]
            ):
                raise RuntimeError(
                    "accepted assignment result differs from saved request"
                )
            if outcome == "redis_sync_pending" and (
                row.get("candidate_accepted") is not True
                or row.get("facts_retained") is not True
            ):
                raise RuntimeError("pending result does not preserve accepted facts")
        elif outcome in {"rejected", "not_found", "not_attempted", "outcome_unknown"}:
            if row.get("ok") is not False:
                raise RuntimeError("failed result row was marked successful")
        else:
            raise RuntimeError("unexpected reassignment result outcome")
    if payload["ok"] is not all(row.get("outcome") == "accepted" for row in rows):
        raise RuntimeError("envelope success does not match target outcomes")
    return data, rows


def _assert_single_assignment(
    payload: object,
    *,
    contract_id: str,
    worker_id: str,
    reason: str,
    previous_assignment_id: str,
) -> dict:
    """Validate the preserved flat envelope for a legacy single-target edit."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"ok", "data", "revision"}
        or payload.get("ok") is not True
        or type(payload.get("revision")) is not int
        or not isinstance(payload.get("data"), dict)
    ):
        raise RuntimeError("single assignment response envelope is malformed")
    data = payload["data"]
    record_id = data.get("record_id")
    if (
        data.get("record_type") != "claim.assignment.changed"
        or data.get("contract_id") != contract_id
        or data.get("worker_id") != worker_id
        or data.get("reason") != reason
        or data.get("previous_assignment_id") != previous_assignment_id
        or data.get("assignment_id") != record_id
        or not isinstance(record_id, str)
        or not record_id
        or not isinstance(data.get("candidate_id"), str)
        or not data["candidate_id"]
        or not isinstance(data.get("idempotency_key"), str)
        or not data["idempotency_key"]
        or data.get("fact_ids") != [record_id]
    ):
        raise RuntimeError("single assignment result does not bind the exact operation")
    return data


def _human_resume_hint_matches(
    hint_stream: str,
    hint_id: str | None,
    reason: str,
    request_names: set[str],
) -> bool:
    """Match the displayed request ID against its persisted filename."""
    return bool(
        hint_id is not None
        and "Resume with: aig -C '" in hint_stream
        and f"task reassign --resume {hint_id}" in hint_stream
        and reason not in hint_stream
        and f"request-{hint_id}.json" in request_names
    )


def installed_reassignment_request_smoke(python: Path, workspace: Path) -> dict:
    """Exercise installed current-head request persistence and exact resume.

    ``workspace`` is a scratch parent; all test workspaces and evidence are
    newly created beneath it. The one process interruption is an explicit
    fixture around the real installed request-save callable: it exits only
    after the original saver has fsynced and published the request, before the
    CLI can return from the save call or invoke its first target.
    """
    python = Path(python).absolute()
    aig = python.parent / "aig"
    scratch = Path(workspace).absolute()
    if not scratch.is_dir():
        raise RuntimeError("scratch parent must already be a directory")
    if not python.is_file() or not aig.is_file():
        raise RuntimeError("installed Python and sibling aig executable are required")

    run_root = scratch / f"installed-reassignment-request-{uuid.uuid4().hex}"
    run_root.mkdir(mode=0o700)
    app_workspace = run_root / "workspace with spaces"
    caller = run_root / "caller"
    evidence = run_root / "private-evidence"
    for directory in (app_workspace, caller, evidence):
        directory.mkdir(mode=0o700)
    store_path = app_workspace / ".aig" / "store.db"
    identity = app_workspace / ".aig" / "identity"
    manifest_path = evidence / "manifest.json"
    local_env = dict(os.environ)
    local_env.pop("PYTHONPATH", None)
    local_env.pop("PYTHONHOME", None)
    local_env["PYTHONNOUSERSITE"] = "1"
    for name in tuple(local_env):
        if name in _PROVIDER_KEYS or name.endswith("_API_KEY"):
            local_env.pop(name, None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")
    redis_url = local_env.get("AIGINEERING_REDIS_URL", "")
    _local_redis(redis_url)

    commands: list[dict] = []
    ids: dict[str, object] = {}
    install_binding: dict[str, str] = {}
    current_phase: str | None = None
    status = "failed"
    error_type: str | None = None
    domain_id: str | None = None
    contracts: dict[str, str] = {}
    request_records: dict[str, dict] = {}
    original_request_bytes: dict[str, bytes] = {}

    replacement_worker = f"worker:request-new-{uuid.uuid4().hex[:12]}"
    original_worker = f"worker:request-old-{uuid.uuid4().hex[:12]}"
    replacement_key = f"worker-key-request-new-{uuid.uuid4().hex[:12]}"
    original_key = f"worker-key-request-old-{uuid.uuid4().hex[:12]}"
    domain_name = f"reassignment-request-{uuid.uuid4().hex}"
    reasons = {
        "single_set": "request current single set",
        "single_clear": "request current single clear",
        "multi_set": "request current multi set",
        "multi_clear": "request current multi clear",
        "interrupt": "request persisted before publication",
        "pending": "request relay recovery control",
        "legacy": "legacy exact assignment control",
        "later": "advance exact assignment head",
        "fresh": "deliberate fresh current request",
    }
    pending_helper = None
    reassignment_helper = None

    def run(
        phase: str,
        argv: list[str],
        *,
        expected_exit: int = 0,
        parse_json: bool = True,
        command_env: dict[str, str] | None = None,
        timeout: float = 90,
    ):
        nonlocal current_phase
        current_phase = phase
        ordinal = len(commands) + 1
        safe_argv = [str(item) for item in argv]
        try:
            process = subprocess.Popen(
                safe_argv,
                cwd=caller,
                env=command_env or local_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            stdout = b""
            stderr = b""
            out_name = f"{ordinal:03d}-{phase}.stdout.raw"
            err_name = f"{ordinal:03d}-{phase}.stderr.raw"
            _write_exclusive(evidence / out_name, stdout)
            _write_exclusive(evidence / err_name, stderr)
            commands.append(
                {
                    "phase": phase,
                    "argv": safe_argv,
                    "exit": None,
                    "spawn_error_type": type(exc).__name__,
                    "timed_out": False,
                    "stdout_path": out_name,
                    "stderr_path": err_name,
                    "stdout_sha256": _sha256(stdout),
                    "stderr_sha256": _sha256(stderr),
                    "expected_exit": expected_exit,
                }
            )
            raise RuntimeError(f"{phase} process could not start") from None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            partial_out = exc.stdout or b""
            partial_err = exc.stderr or b""
            if isinstance(partial_out, str):
                partial_out = partial_out.encode()
            if isinstance(partial_err, str):
                partial_err = partial_err.encode()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            tail_out, tail_err = process.communicate(timeout=10)
            stdout = tail_out or partial_out
            stderr = tail_err or partial_err
            timed_out = True
        out_name = f"{ordinal:03d}-{phase}.stdout.raw"
        err_name = f"{ordinal:03d}-{phase}.stderr.raw"
        _write_exclusive(evidence / out_name, stdout)
        _write_exclusive(evidence / err_name, stderr)
        commands.append(
            {
                "phase": phase,
                "argv": safe_argv,
                "exit": process.returncode,
                "timed_out": timed_out,
                "stdout_path": out_name,
                "stderr_path": err_name,
                "stdout_sha256": _sha256(stdout),
                "stderr_sha256": _sha256(stderr),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
                "expected_exit": expected_exit,
            }
        )
        if timed_out:
            raise RuntimeError(f"{phase} process timed out")
        if process.returncode != expected_exit:
            raise RuntimeError(f"{phase} returned an unexpected exit code")
        if not parse_json:
            return subprocess.CompletedProcess(
                safe_argv, process.returncode, stdout, stderr
            ), None
        try:
            payload = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(f"{phase} did not return JSON") from None
        return subprocess.CompletedProcess(
            safe_argv, process.returncode, stdout, stderr
        ), payload

    def cli(phase: str, *args: str, expected_exit: int = 0, command_env=None):
        return run(
            phase,
            [str(aig), "-C", str(app_workspace), *args, "--json"],
            expected_exit=expected_exit,
            command_env=command_env,
        )

    def fingerprint(phase: str, *, store: Path = store_path) -> dict:
        _completed, payload = run(
            phase, [str(python), "-c", pending_helper._FINGERPRINT, str(store)]
        )
        if not isinstance(payload, dict) or set(payload.get("tables", {})) != _TABLES:
            raise RuntimeError("Store fingerprint omitted one of the 18 tables")
        return payload

    def identity_fingerprint(identity_root: Path = identity) -> dict[str, str]:
        if not identity_root.exists():
            return {}
        return {
            path.relative_to(identity_root).as_posix(): _sha256(path.read_bytes())
            for path in sorted(identity_root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

    def read_assignments(phase: str, target_ids: tuple[str, ...]) -> dict:
        _completed, payload = run(
            phase,
            [
                str(python),
                "-c",
                reassignment_helper._READ_ASSIGNMENTS,
                str(store_path),
                *target_ids,
            ],
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("assignments"), dict)
            or set(payload["assignments"]) != set(target_ids)
        ):
            raise RuntimeError("assignment projection omitted a requested target")
        return payload["assignments"]

    def prove_assignment(
        phase: str,
        contract_id: str,
        record_id: str,
        candidate_id: str,
        previous: str,
        worker_id: str,
    ) -> dict:
        before = fingerprint(f"{phase}-before")
        _completed, proof = run(
            phase,
            [
                str(python),
                "-c",
                pending_helper._PROVE_FACT,
                str(store_path),
                "assignment",
                contract_id,
                record_id,
                candidate_id,
                previous,
                worker_id,
            ],
        )
        after = fingerprint(f"{phase}-after")
        if before != after or not isinstance(proof, dict):
            raise RuntimeError("assignment proof was not read-only or well formed")
        if (
            proof.get("record_id") != record_id
            or proof.get("candidate_id") != candidate_id
            or proof.get("contract_id") != contract_id
            or proof.get("previous") != previous
            or proof.get("value") != worker_id
            or proof.get("record_type") != "claim.assignment.changed"
        ):
            raise RuntimeError("exact assignment receipt/head proof differs")
        return proof

    def feed_proof(phase: str, record_id: str, after_revision: int) -> dict:
        _completed, payload = run(
            phase,
            [
                str(python),
                "-c",
                pending_helper._FEED_PROOF,
                domain_id,
                record_id,
                "claim.assignment.changed",
                str(after_revision),
            ],
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("matches"), list
        ):
            raise RuntimeError("Redis assignment feed proof is malformed")
        return payload

    def inspect_request(
        phase: str, request_id: str, *, store: Path = store_path
    ) -> dict:
        _completed, payload = run(
            phase,
            [str(python), "-c", _READ_REQUEST, str(store), request_id],
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("request"), dict
        ):
            raise RuntimeError(
                "saved request inspection did not return canonical fields"
            )
        request = payload["request"]
        if (
            payload.get("mode") != 0o600
            or payload.get("directory_mode") != 0o700
            or request.get("request_id") != request_id
            or payload.get("file_sha256")
            != _sha256(_canonical_request(request).encode("utf-8"))
        ):
            raise RuntimeError("saved request file metadata/digest differs")
        return request

    def invoke_current(
        phase: str,
        target_ids: tuple[str, ...],
        *,
        worker_id: str | None,
        clear: bool,
        reason: str,
        idempotency_key: str | None = None,
        expected_exit: int = 0,
        command_env=None,
    ):
        args = ["task", "reassign", *target_ids, "--current"]
        if clear:
            args.append("--clear")
        else:
            args.extend(("--worker", worker_id or ""))
        args.extend(("--reason", reason))
        if idempotency_key is not None:
            args.extend(("--idempotency-key", idempotency_key))
        _completed, payload = cli(
            phase, *args, expected_exit=expected_exit, command_env=command_env
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        request_id = data.get("request_id") if isinstance(data, dict) else None
        if not isinstance(request_id, str) or len(request_id) != 64:
            raise RuntimeError("current reassignment result omitted request ID")
        if re.fullmatch(r"[0-9a-f]{64}", request_id) is None:
            raise RuntimeError(
                "current reassignment request ID is not canonical lowercase hex"
            )
        return payload, request_id

    def resume(phase: str, request_id: str, *, expected_exit: int = 0):
        return cli(
            phase,
            "task",
            "reassign",
            "--resume",
            request_id,
            expected_exit=expected_exit,
        )

    def request_names(phase: str, *, store: Path = store_path) -> set[str]:
        _completed, payload = run(phase, [str(python), "-c", _REQUEST_ENUM, str(store)])
        names = payload.get("names") if isinstance(payload, dict) else None
        if not isinstance(names, list) or any(
            not isinstance(name, str) for name in names
        ):
            raise RuntimeError(
                "installed request enumeration returned an invalid shape"
            )
        return set(names)

    def resume_in(
        phase: str,
        target_workspace: Path,
        request_id: str,
        *,
        expected_exit: int = 0,
        command_env=None,
    ):
        return run(
            phase,
            [
                str(aig),
                "-C",
                str(target_workspace),
                "task",
                "reassign",
                "--resume",
                request_id,
                "--json",
            ],
            expected_exit=expected_exit,
            command_env=command_env,
        )

    def create_contract(label: str, *, assigned: bool = True) -> str:
        _completed, created = cli(
            f"task-create-{label}",
            "task",
            "create",
            "--name",
            f"request-{label}-{uuid.uuid4().hex[:8]}",
            "--description",
            f"Installed reassignment request fixture {label}.",
            "--budget",
            "3",
        )
        contract_id = created.get("contract_id") if isinstance(created, dict) else None
        if not isinstance(contract_id, str) or not contract_id:
            raise RuntimeError("task create omitted Contract ID")
        contracts[label] = contract_id
        if assigned:
            _completed, result = cli(
                f"initial-assignment-{label}",
                "task",
                "assign",
                contract_id,
                original_worker,
                "--reason",
                "request fixture initial assignment",
            )
            operation = _assert_single_assignment(
                result,
                contract_id=contract_id,
                worker_id=original_worker,
                reason="request fixture initial assignment",
                previous_assignment_id="",
            )
            prove_assignment(
                f"initial-assignment-proof-{label}",
                contract_id,
                operation["record_id"],
                operation["candidate_id"],
                "",
                original_worker,
            )
            return operation["record_id"]
        return ""

    def verify_saved_request(
        phase: str,
        request_id: str,
        *,
        targets: tuple[str, ...],
        previous: dict[str, str],
        action: str,
        worker_id: str | None,
        reason: str,
        batch_base: str,
        observed_revision: int,
        store: Path = store_path,
    ) -> dict:
        request = inspect_request(phase, request_id, store=store)
        _assert_request(
            request,
            request_id=request_id,
            store_path=store,
            domain_id=domain_id,
            targets=targets,
            previous=previous,
            action=action,
            worker_id=worker_id,
            reason=reason,
            batch_base=batch_base,
            observed_revision=observed_revision,
        )
        return request

    status = "failed"
    try:
        pending_helper, reassignment_helper = _load_pinned_helpers()
        origin_process, origin = run(
            "installed-origin",
            [str(python), "-c", _INSTALL_ORIGIN],
        )
        install_binding = _validate_origin(python, aig, origin)
        identity.mkdir(parents=True, mode=0o700, exist_ok=True)
        _completed, domain = cli(
            "domain-init", "domain", "init", "--domain", domain_name
        )
        if not store_path.is_file():
            raise RuntimeError("domain initialization omitted isolated Store")
        domain_id = domain.get("domain_id") if isinstance(domain, dict) else None
        if not isinstance(domain_id, str) or not domain_id:
            raise RuntimeError("domain initialization omitted Genesis ID")
        ids.update({"domain_name": domain_name, "domain_id": domain_id})

        for worker_id, key_id, phase in (
            (replacement_worker, replacement_key, "replacement-worker"),
            (original_worker, original_key, "original-worker"),
        ):
            _completed, public = run(
                f"{phase}-public-key", [str(python), "-c", _WORKER_PUBLIC_KEY]
            )
            if not isinstance(public, dict) or not isinstance(
                public.get("public_key"), str
            ):
                raise RuntimeError("Worker public key fixture was not generated")
            cli(
                f"{phase}-register",
                "worker",
                "register",
                "--worker-id",
                worker_id,
                "--capacity",
                "16",
                "--key-id",
                key_id,
                "--public-key",
                public["public_key"],
            )

        initial_heads: dict[str, str] = {}
        labels = (
            "single-set",
            "single-clear",
            "multi-set-1",
            "multi-set-2",
            "multi-clear-1",
            "multi-clear-2",
            "no-history",
            "interrupt-first",
            "interrupt-second",
            "pending-first",
            "pending-middle",
            "pending-last",
            "legacy-single",
        )
        for label in labels:
            head = create_contract(label, assigned=label != "no-history")
            if head:
                initial_heads[contracts[label]] = head
        ids["contracts"] = contracts
        ids["initial_heads"] = initial_heads

        # Current-head single and homogeneous multi set/clear each persist a
        # complete request before normal per-target publication.
        operations = (
            (
                "single-set",
                (contracts["single-set"],),
                False,
                replacement_worker,
                reasons["single_set"],
            ),
            (
                "single-clear",
                (contracts["single-clear"],),
                True,
                None,
                reasons["single_clear"],
            ),
            (
                "multi-set",
                (contracts["multi-set-1"], contracts["multi-set-2"]),
                False,
                replacement_worker,
                reasons["multi_set"],
            ),
            (
                "multi-clear",
                (contracts["multi-clear-1"], contracts["multi-clear-2"]),
                True,
                None,
                reasons["multi_clear"],
            ),
        )
        current_requests: dict[str, dict] = {}
        for label, target_ids, clear, worker_id, reason in operations:
            before = fingerprint(f"{label}-before-current")
            prev = {target: initial_heads[target] for target in target_ids}
            payload, request_id = invoke_current(
                f"{label}-current",
                target_ids,
                worker_id=worker_id,
                clear=clear,
                reason=reason,
                idempotency_key=f"explicit-{label}-base"
                if label == "multi-set"
                else None,
            )
            payload_data = payload.get("data") if isinstance(payload, dict) else None
            data_base = (
                payload_data.get("batch_idempotency_key")
                if isinstance(payload_data, dict)
                else None
            )
            data, rows = _assert_result(
                payload,
                request_id=request_id,
                targets=target_ids,
                previous=prev,
                worker_id="" if clear else (worker_id or ""),
                reason=reason,
                expected_batch_base=(
                    "explicit-multi-set-base" if label == "multi-set" else data_base
                ),
            )
            if (
                not isinstance(data.get("batch_idempotency_key"), str)
                or not data["batch_idempotency_key"]
            ):
                raise RuntimeError(f"{label} omitted its frozen batch key")
            if (
                label == "multi-set"
                and data["batch_idempotency_key"] != "explicit-multi-set-base"
            ):
                raise RuntimeError("explicit current batch key was not preserved")
            if [row.get("outcome") for row in rows] != ["accepted"] * len(target_ids):
                raise RuntimeError(f"{label} did not accept every independent target")
            request = verify_saved_request(
                f"{label}-saved-request",
                request_id,
                targets=target_ids,
                previous=prev,
                action="clear" if clear else "set",
                worker_id=None if clear else worker_id,
                reason=reason,
                batch_base=data["batch_idempotency_key"],
                observed_revision=before["revision"],
            )
            request_path = _request_file(store_path, request_id)
            original_request_bytes[request_id] = request_path.read_bytes()
            current_requests[label] = request
            request_records[label] = {
                "id": request_id,
                "request": request,
                "rows": rows,
            }
            for row in rows:
                proof = prove_assignment(
                    f"{label}-assignment-proof-{row['contract_id']}",
                    row["contract_id"],
                    row["data"]["record_id"],
                    row["candidate_id"],
                    prev[row["contract_id"]],
                    row["data"]["worker_id"],
                )
                ids.setdefault("assignment_facts", []).append(proof["record_id"])

        # No-history is a global preparation refusal: no request, identity or
        # Store row changes, and no implicit switch to initial assignment.
        no_history_before = fingerprint("no-history-current-before")
        no_history_identity = identity_fingerprint()
        requests_before = sorted(request_names("no-history-request-list-before"))
        no_history_payload = None
        try:
            _completed, no_history_payload = cli(
                "no-history-current-refusal",
                "task",
                "reassign",
                contracts["single-set"],
                contracts["no-history"],
                "--current",
                "--worker",
                replacement_worker,
                "--reason",
                "no assignment head must fail globally",
                expected_exit=1,
            )
        finally:
            no_history_after = fingerprint("no-history-current-after")
            no_history_identity_after = identity_fingerprint()
        requests_after = sorted(request_names("no-history-request-list-after"))
        if (
            _json_error_code(pending_helper, no_history_payload)
            != "assignment_head_missing"
            or no_history_before != no_history_after
            or no_history_identity != no_history_identity_after
            or requests_before != requests_after
        ):
            raise RuntimeError("no-history preparation was not globally write-neutral")

        # Test malformed, missing, symlink, truncated/tampered request files
        # against a completed request while preserving its original bytes.
        probe_id = request_records["single-set"]["id"]
        probe_path = _request_file(store_path, probe_id)
        probe_raw = original_request_bytes[probe_id]
        missing_id = "0" * 64
        if missing_id == probe_id:
            missing_id = "1" * 64
        for phase, request_id, code in (
            ("missing-request-refusal", missing_id, "request_not_found"),
        ):
            before = fingerprint(f"{phase}-before")
            identity_before = identity_fingerprint()
            _completed, payload = cli(
                phase, "task", "reassign", "--resume", request_id, expected_exit=1
            )
            after = fingerprint(f"{phase}-after")
            if (
                _json_error_code(pending_helper, payload) != code
                or before != after
                or identity_before != identity_fingerprint()
            ):
                raise RuntimeError(f"{phase} wrote state or returned the wrong code")

        corrupt_id = f"{uuid.uuid4().hex}{uuid.uuid4().hex}"
        corrupt_id = corrupt_id[:64]
        corrupt_path = _request_file(store_path, corrupt_id)
        corrupt_path.symlink_to(probe_path)
        try:
            before = fingerprint("symlink-request-before")
            _completed, symlink_payload = cli(
                "symlink-request-refusal",
                "task",
                "reassign",
                "--resume",
                corrupt_id,
                expected_exit=1,
            )
            after = fingerprint("symlink-request-after")
            if (
                _json_error_code(pending_helper, symlink_payload) != "request_invalid"
                or before != after
            ):
                raise RuntimeError("symlink request was not safely refused")
        finally:
            corrupt_path.unlink(missing_ok=True)

        try:
            probe_path.write_bytes(probe_raw[:-1])
            before = fingerprint("truncated-request-before")
            _completed, truncated_payload = cli(
                "truncated-request-refusal",
                "task",
                "reassign",
                "--resume",
                probe_id,
                expected_exit=1,
            )
            after = fingerprint("truncated-request-after")
            if (
                _json_error_code(pending_helper, truncated_payload) != "request_invalid"
                or before != after
            ):
                raise RuntimeError("truncated request was not safely refused")
        finally:
            probe_path.write_bytes(probe_raw)
            os.chmod(probe_path, 0o600)

        # Path mismatch is tested by copying an immutable original request to a
        # second initialized workspace. Domain mismatch uses a hash-consistent
        # body at the second Store path but retains the first Genesis ID.
        second_workspace = run_root / "other workspace"
        second_caller = run_root / "other caller"
        second_workspace.mkdir(mode=0o700)
        second_caller.mkdir(mode=0o700)
        second_store_path = second_workspace / ".aig" / "store.db"
        second_identity = second_workspace / ".aig" / "identity"
        second_identity.mkdir(parents=True, mode=0o700)
        path_env = dict(local_env)
        path_env["AIG_ACTOR_KEY_FILE"] = str(second_identity / "root.ed25519")
        _completed, second_domain = run(
            "other-domain-init",
            [
                str(aig),
                "-C",
                str(second_workspace),
                "domain",
                "init",
                "--domain",
                f"other-{uuid.uuid4().hex}",
                "--json",
            ],
            command_env=path_env,
        )
        second_domain_id = (
            second_domain.get("domain_id") if isinstance(second_domain, dict) else None
        )
        if not isinstance(second_domain_id, str) or second_domain_id == domain_id:
            raise RuntimeError("second workspace did not create a different Genesis")
        second_request_dir = second_store_path.parent / "requests" / "reassign"
        second_request_dir.mkdir(parents=True, mode=0o700)
        os.chmod(second_store_path.parent / "requests", 0o700)
        os.chmod(second_request_dir, 0o700)
        copied = second_request_dir / f"request-{probe_id}.json"
        _write_exclusive(copied, probe_raw)
        before = fingerprint("cross-workspace-store-before", store=second_store_path)
        _completed, path_mismatch = resume_in(
            "cross-workspace-request-refusal",
            second_workspace,
            probe_id,
            expected_exit=1,
            command_env=path_env,
        )
        after = fingerprint("cross-workspace-store-after", store=second_store_path)
        if (
            _json_error_code(pending_helper, path_mismatch)
            != "request_workspace_mismatch"
            or before != after
        ):
            raise RuntimeError("copied request did not fail workspace binding")
        # Test the independent domain check with a canonical, correctly
        # addressed fixture whose saved Store path matches the second workspace.
        rehash_dir = second_request_dir / "domain-check"
        rehash_dir.mkdir(mode=0o700)
        _completed, forged = run(
            "domain-mismatch-fixture-create",
            [
                str(python),
                "-c",
                _REHASH_REQUEST,
                str(probe_path),
                str(second_store_path),
                str(rehash_dir),
            ],
        )
        domain_mismatch_id = forged.get("request_id")
        if not isinstance(domain_mismatch_id, str) or len(domain_mismatch_id) != 64:
            raise RuntimeError("domain mismatch fixture omitted its content digest")
        # Move the correctly hashed fixture to the exact request root used by
        # the CLI; the body remains a no-signature input, not authorization.
        nested_fixture = rehash_dir / f"request-{domain_mismatch_id}.json"
        final_fixture = second_request_dir / nested_fixture.name
        os.link(nested_fixture, final_fixture)
        before = fingerprint("domain-mismatch-store-before", store=second_store_path)
        _completed, domain_mismatch = resume_in(
            "cross-domain-request-refusal",
            second_workspace,
            domain_mismatch_id,
            expected_exit=1,
            command_env=path_env,
        )
        after = fingerprint("domain-mismatch-store-after", store=second_store_path)
        if (
            _json_error_code(pending_helper, domain_mismatch)
            != "request_domain_mismatch"
            or before != after
        ):
            raise RuntimeError("different-Genesis request was not safely refused")
        nested_fixture.unlink(missing_ok=True)
        final_fixture.unlink(missing_ok=True)
        rehash_dir.rmdir()

        # Deterministic prepared-only process stop at the installed CLI's real
        # saver. The original function returns only after file+directory fsync.
        interrupted_targets = (
            contracts["interrupt-first"],
            contracts["interrupt-second"],
        )
        interrupted_heads = {
            target: initial_heads[target] for target in interrupted_targets
        }
        before_interrupt = fingerprint("prepared-only-before")
        identity_before_interrupt = identity_fingerprint()
        before_request_names = request_names("prepared-only-request-list-before")
        interrupt_args = [
            "-C",
            str(app_workspace),
            "task",
            "reassign",
            *interrupted_targets,
            "--current",
            "--worker",
            replacement_worker,
            "--reason",
            reasons["interrupt"],
            "--json",
        ]
        _completed, _no_payload = run(
            "prepared-only-save-then-process-exit",
            [str(python), "-c", _INTERRUPT_AFTER_SAVE, *interrupt_args],
            expected_exit=86,
            parse_json=False,
        )
        after_interrupt = fingerprint("prepared-only-after-exit")
        identity_after_interrupt = identity_fingerprint()
        names_after_interrupt = request_names("prepared-only-request-list-after")
        new_names = names_after_interrupt - before_request_names
        if (
            before_interrupt != after_interrupt
            or identity_before_interrupt != identity_after_interrupt
            or len(new_names) != 1
        ):
            raise RuntimeError(
                "prepared-only process exit published or lost request file"
            )
        interrupted_id = (
            next(iter(new_names)).removeprefix("request-").removesuffix(".json")
        )
        interrupted_base = None
        interrupted_request = inspect_request(
            "prepared-only-request-inspection", interrupted_id
        )
        interrupted_base = interrupted_request["batch_base"]
        interrupted_request = _assert_request(
            interrupted_request,
            request_id=interrupted_id,
            store_path=store_path,
            domain_id=domain_id,
            targets=interrupted_targets,
            previous=interrupted_heads,
            action="set",
            worker_id=replacement_worker,
            reason=reasons["interrupt"],
            batch_base=interrupted_base,
            observed_revision=before_interrupt["revision"],
        )
        interrupted_path = _request_file(store_path, interrupted_id)
        original_request_bytes[interrupted_id] = interrupted_path.read_bytes()
        request_records["interrupted"] = {
            "id": interrupted_id,
            "request": interrupted_request,
        }
        ids["prepared_only_request_id"] = interrupted_id

        # Move the second head after snapshot/save but before resuming. The
        # original request remains immutable: first can accept; second rejects.
        _completed, raced_head = cli(
            "advance-interrupted-second-before-resume",
            "task",
            "reassign",
            interrupted_targets[1],
            "--worker",
            replacement_worker,
            "--previous",
            interrupted_heads[interrupted_targets[1]],
            "--reason",
            reasons["later"],
        )
        raced_operation = (
            raced_head.get("data") if isinstance(raced_head, dict) else None
        )
        if not isinstance(raced_operation, dict):
            raise RuntimeError("concurrent-head fixture did not create a later head")
        prove_assignment(
            "advanced-request-target-proof",
            interrupted_targets[1],
            raced_operation["record_id"],
            raced_operation["candidate_id"],
            interrupted_heads[interrupted_targets[1]],
            replacement_worker,
        )
        first_resume_before = fingerprint("prepared-only-resume-before")
        _completed, first_resume = resume(
            "prepared-only-fresh-process-resume", interrupted_id, expected_exit=1
        )
        first_resume_after = fingerprint("prepared-only-resume-after")
        first_data, first_rows = _assert_result(
            first_resume,
            request_id=interrupted_id,
            targets=interrupted_targets,
            previous=interrupted_heads,
            worker_id=replacement_worker,
            reason=reasons["interrupt"],
            expected_batch_base=interrupted_base,
        )
        if (
            [row["outcome"] for row in first_rows] != ["accepted", "rejected"]
            or first_resume_before["revision"] >= first_resume_after["revision"]
            or first_rows[1].get("data") is not None
            or interrupted_path.read_bytes() != original_request_bytes[interrupted_id]
        ):
            raise RuntimeError("resume refreshed or rewrote its saved exact heads")
        first_original = first_rows[0]
        prove_assignment(
            "prepared-only-first-accepted-proof",
            interrupted_targets[0],
            first_original["data"]["record_id"],
            first_original["candidate_id"],
            interrupted_heads[interrupted_targets[0]],
            replacement_worker,
        )

        # Advance that accepted member after its first commit; old request replay
        # returns the original fact while the still-stale second member refuses.
        _completed, later_first = cli(
            "advance-committed-request-member",
            "task",
            "reassign",
            interrupted_targets[0],
            "--worker",
            original_worker,
            "--previous",
            first_original["data"]["record_id"],
            "--reason",
            reasons["later"],
        )
        later_first_data = (
            later_first.get("data") if isinstance(later_first, dict) else None
        )
        if not isinstance(later_first_data, dict):
            raise RuntimeError("later-head fixture did not commit")
        prove_assignment(
            "later-head-after-original-proof",
            interrupted_targets[0],
            later_first_data["record_id"],
            later_first_data["candidate_id"],
            first_original["data"]["record_id"],
            original_worker,
        )
        replay_before = fingerprint("old-request-replay-after-later-head-before")
        _completed, replay_after_later = resume(
            "old-request-replay-after-later-head", interrupted_id, expected_exit=1
        )
        replay_after = fingerprint("old-request-replay-after-later-head-after")
        replay_data, replay_rows = _assert_result(
            replay_after_later,
            request_id=interrupted_id,
            targets=interrupted_targets,
            previous=interrupted_heads,
            worker_id=replacement_worker,
            reason=reasons["interrupt"],
            expected_batch_base=interrupted_base,
        )
        if (
            replay_before != replay_after
            or replay_rows[0]["data"] != first_original["data"]
            or replay_rows[1]["outcome"] != "rejected"
            or interrupted_path.read_bytes() != original_request_bytes[interrupted_id]
        ):
            raise RuntimeError(
                "old request replay changed facts or refreshed stale heads"
            )

        # A deliberate new current-head request is a distinct intent and uses
        # the second target's newer exact head instead of rewriting old request.
        fresh_before = fingerprint("fresh-current-after-old-resume-before")
        fresh_payload, fresh_id = invoke_current(
            "fresh-current-after-old-resume",
            (interrupted_targets[1],),
            worker_id=original_worker,
            clear=False,
            reason=reasons["fresh"],
        )
        fresh_data, fresh_rows = _assert_result(
            fresh_payload,
            request_id=fresh_id,
            targets=(interrupted_targets[1],),
            previous={interrupted_targets[1]: raced_operation["record_id"]},
            worker_id=original_worker,
            reason=reasons["fresh"],
            expected_batch_base=fresh_payload["data"]["batch_idempotency_key"],
        )
        verify_saved_request(
            "fresh-current-request-inspection",
            fresh_id,
            targets=(interrupted_targets[1],),
            previous={interrupted_targets[1]: raced_operation["record_id"]},
            action="set",
            worker_id=original_worker,
            reason=reasons["fresh"],
            batch_base=fresh_data["batch_idempotency_key"],
            observed_revision=fresh_before["revision"],
        )
        if fresh_id == interrupted_id or fresh_rows[0]["outcome"] != "accepted":
            raise RuntimeError("fresh current-head request reused old intent")
        ids["fresh_after_stale_request_id"] = fresh_id

        # Legacy explicit single syntax remains available with its exact head;
        # its output remains the old direct success shape without request_id.
        legacy_contract = contracts["legacy-single"]
        legacy_previous = initial_heads[legacy_contract]
        _completed, legacy = cli(
            "legacy-explicit-reassign",
            "task",
            "reassign",
            legacy_contract,
            "--worker",
            replacement_worker,
            "--previous",
            legacy_previous,
            "--reason",
            reasons["legacy"],
        )
        legacy_data = _assert_single_assignment(
            legacy,
            contract_id=legacy_contract,
            worker_id=replacement_worker,
            reason=reasons["legacy"],
            previous_assignment_id=legacy_previous,
        )
        if "request_id" in legacy:
            raise RuntimeError("legacy exact reassignment output changed")
        prove_assignment(
            "legacy-explicit-fact-proof",
            legacy_contract,
            legacy_data["record_id"],
            legacy_data["candidate_id"],
            legacy_previous,
            replacement_worker,
        )

        # Sustained assignment relay refusal, persisted current request, then
        # exact normal resume and catch-up for all three seeded targets.
        pending_targets = tuple(
            contracts[label]
            for label in ("pending-first", "pending-middle", "pending-last")
        )
        pending_previous = {target: initial_heads[target] for target in pending_targets}
        pending_seeded = read_assignments("pending-seeded-before", pending_targets)
        pending_before = fingerprint("pending-current-before")
        server, server_thread, proxy_url = pending_helper._relay_proxy(redis_url)
        proxy_env = dict(local_env)
        proxy_env["AIGINEERING_REDIS_URL"] = proxy_url
        try:
            pending_payload, pending_id = invoke_current(
                "pending-current-relay-fault",
                pending_targets,
                worker_id=replacement_worker,
                clear=False,
                reason=reasons["pending"],
                expected_exit=1,
                command_env=proxy_env,
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        if not server.faults or any(
            kind != "claim.assignment.changed" for kind in server.faults
        ):
            raise RuntimeError("relay proxy did not target only assignment appends")
        pending_info = pending_payload.get("data")
        if not isinstance(pending_info, dict):
            raise RuntimeError("relay-pending current request omitted data envelope")
        pending_data = pending_info
        pending_rows = pending_data.get("results")
        if not isinstance(pending_rows, list) or [
            row.get("outcome") for row in pending_rows
        ] != ["redis_sync_pending", "not_attempted", "not_attempted"]:
            raise RuntimeError(
                "sustained relay fault did not preserve pending/tail outcomes"
            )
        _assert_result(
            pending_payload,
            request_id=pending_id,
            targets=pending_targets,
            previous=pending_previous,
            worker_id=replacement_worker,
            reason=reasons["pending"],
            expected_batch_base=pending_data["batch_idempotency_key"],
        )
        request = verify_saved_request(
            "pending-request-file",
            pending_id,
            targets=pending_targets,
            previous=pending_previous,
            action="set",
            worker_id=replacement_worker,
            reason=reasons["pending"],
            batch_base=pending_data["batch_idempotency_key"],
            observed_revision=pending_before["revision"],
        )
        pending_path = _request_file(store_path, pending_id)
        pending_raw = pending_path.read_bytes()
        pending_after = fingerprint("pending-after-commit-before-recovery")
        if pending_after["revision"] <= pending_before["revision"]:
            raise RuntimeError(
                "accepted pending assignment did not commit a Store revision"
            )
        pending_projection = read_assignments(
            "pending-projection-before-recovery", pending_targets
        )
        pending_first = pending_rows[0]
        reassignment_helper._assert_pending_projection(
            pending_seeded,
            pending_projection,
            contract_ids=pending_targets,
            first_contract_id=pending_targets[0],
            accepted_data=pending_first["data"],
        )
        prove_assignment(
            "pending-first-durable-proof",
            pending_targets[0],
            pending_first["data"]["record_id"],
            pending_first["candidate_id"],
            pending_previous[pending_targets[0]],
            replacement_worker,
        )
        absent = feed_proof(
            "pending-first-feed-absent-before-recovery",
            pending_first["data"]["record_id"],
            pending_before["revision"],
        )
        if absent["matches"]:
            raise RuntimeError(
                "pending direct fact appeared in the feed before recovery"
            )
        pending_resume_before = fingerprint("pending-resume-before")
        _completed, pending_resume = resume("pending-resume-after-restore", pending_id)
        pending_resume_after = fingerprint("pending-resume-after")
        _resume_data, resumed_rows = _assert_result(
            pending_resume,
            request_id=pending_id,
            targets=pending_targets,
            previous=pending_previous,
            worker_id=replacement_worker,
            reason=reasons["pending"],
            expected_batch_base=pending_data["batch_idempotency_key"],
        )
        if (
            [row["outcome"] for row in resumed_rows] != ["accepted"] * 3
            or resumed_rows[0]["data"] != pending_first["data"]
            or pending_path.read_bytes() != pending_raw
            or pending_resume_after["revision"] <= pending_resume_before["revision"]
        ):
            raise RuntimeError("request resume did not catch up exact pending batch")
        for original, resumed in zip(pending_rows, resumed_rows, strict=True):
            row = resumed
            proof = prove_assignment(
                f"pending-resumed-proof-{row['contract_id']}",
                row["contract_id"],
                row["data"]["record_id"],
                row["candidate_id"],
                pending_previous[row["contract_id"]],
                replacement_worker,
            )
            feed = feed_proof(
                f"pending-resumed-feed-{row['contract_id']}",
                row["data"]["record_id"],
                pending_before["revision"],
            )
            if (
                len(feed["matches"]) != 1
                or feed["matches"][0].get("record_id") != row["data"]["record_id"]
            ):
                raise RuntimeError("resumed assignment did not relay exactly once")

        # Human request hints are safe and workspace paths with spaces are
        # quoted; output is captured privately rather than parsed as JSON.
        hint_target = contracts["multi-set-1"]
        hint_reason = "safe human hint output"
        hint_cmd = [
            str(aig),
            "-C",
            str(app_workspace),
            "task",
            "reassign",
            hint_target,
            "--current",
            "--worker",
            original_worker,
            "--reason",
            hint_reason,
        ]
        _completed, _unused = run(
            "human-current-resume-hint", hint_cmd, parse_json=False
        )
        hint_stream = (
            evidence / f"{len(commands):03d}-human-current-resume-hint.stderr.raw"
        ).read_text(errors="replace")
        hint_match = re.search(
            r"Saved reassignment request ([0-9a-f]{64})\.", hint_stream
        )
        hint_id = hint_match.group(1) if hint_match else None
        human_request_names = request_names("human-request-list-after")
        if not _human_resume_hint_matches(
            hint_stream, hint_id, hint_reason, human_request_names
        ):
            raise RuntimeError(
                "human resume hint is missing, unquoted, or contains reason text"
            )

        all_targets = tuple(contracts.values())
        projection_before = read_assignments(
            "all-assignment-projection-before-rebuild", all_targets
        )
        rebuild_before = fingerprint("rebuild-before")
        _completed, rebuilt = run(
            "runtime-rebuild",
            [str(python), "-c", reassignment_helper._REBUILD, str(store_path)],
        )
        rebuild_after = fingerprint("rebuild-after")
        projection_after = read_assignments(
            "all-assignment-projection-after-rebuild", all_targets
        )
        if (
            projection_before != projection_after
            or rebuilt.get("before") != rebuilt.get("after")
            or rebuild_before["tables"]["runtime_records"]
            != rebuild_after["tables"]["runtime_records"]
        ):
            raise RuntimeError(
                "reopen/rebuild changed assignment history or head projection"
            )

        status = "complete"
        ids.update(
            {
                "request_ids": {
                    key: value["id"] for key, value in request_records.items()
                },
                "all_request_ids": sorted(
                    name.removeprefix("request-").removesuffix(".json")
                    for name in human_request_names
                ),
                "prepared_only_request_id": interrupted_id,
                "pending_request_id": pending_id,
                "pending_assignment_ids": [
                    row["data"]["record_id"] for row in resumed_rows
                ],
                "pending_candidate_ids": [row["candidate_id"] for row in resumed_rows],
                "pending_fault_count": len(server.faults),
                "pending_feed_absent_before_restore": len(absent["matches"]) == 0,
                "worker_invocations": 0,
                "provider_calls": 0,
                "source_only_controls_not_run": [
                    "64-target current request",
                    "current authorization/key revocation",
                    "true concurrent writer race at snapshot boundary",
                ],
                "store_table_count": len(rebuild_after["tables"]),
                "final_revision": rebuild_after["revision"],
            }
        )
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        manifest = {
            "schema": "aigineering.installed-reassignment-request.v1",
            "status": status,
            "current_phase": current_phase,
            "helper_sha256": _sha256(Path(__file__).read_bytes()),
            "workspace": str(app_workspace),
            "store_path": str(store_path),
            "domain_name": domain_name,
            "domain_id": domain_id,
            "redis_database": "12",
            "provider_invoked": False,
            "worker_invoked": False,
            "provider_credentials_in_child_env": False,
            "source_binding": "same venv/site-packages checked here; caller checks actual source checkout exclusion",
            "install_binding": install_binding,
            "ids": ids,
            "error_type": error_type,
            "commands": commands,
        }
        _write_exclusive(
            manifest_path,
            (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        )
    if status != "complete":
        raise RuntimeError("installed reassignment request control did not complete")
    return {
        "status": status,
        "evidence_dir": str(evidence),
        "domain_name": domain_name,
        "contract_count": len(contracts),
        "request_count": len(ids["all_request_ids"]),
        "pending_fact_count": len(ids["pending_assignment_ids"]),
        "worker_invocations": 0,
        "provider_calls": 0,
    }
