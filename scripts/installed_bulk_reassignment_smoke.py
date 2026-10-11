"""Source-external installed controls for exact-head bulk reassignment.

The root acceptance harness calls :func:`installed_bulk_reassignment_smoke`
with the packaged CLI and interpreter. The helper creates an isolated domain,
ordinary task declarations, and idle registered Workers. It never invokes a
Worker or provider. All command streams and Store/Redis proof output remain in a
private evidence directory; the returned value contains safe IDs only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import uuid


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

_PENDING_HELPER_SHA256 = (
    "665d4d6dbe8bf57ae941cc26a777441a8411aee6212049b31223ca8d02482a4f"
)
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

_WORKER_PUBLIC_KEY = r"""
import json
from aigineering.core.signing import Ed25519Signer
signer=Ed25519Signer()
print(json.dumps({'public_key':signer.signer_id,'signature_kind':signer.kind}))
"""

_INSTALL_ORIGIN = r"""
import json
import pathlib
import sys
import aigineering
print(json.dumps({
    'prefix': str(pathlib.Path(sys.prefix).absolute()),
    'module': str(pathlib.Path(aigineering.__file__).absolute()),
}))
"""

_READ_ASSIGNMENTS = r"""
import json,sys
from aigineering.core.claim_assignments import (
    assignment_context_from_projection, assignment_head, assignment_history,
    effective_assignment,
)
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.sqlite_store import SQLiteStore
path,*contract_ids=sys.argv[1:]
store=SQLiteStore.snapshot_from_path(path)
try:
    context=assignment_context_from_projection(load_effect_projection_context(store))
    result={}
    for contract_id in contract_ids:
        history=assignment_history(context,contract_id=contract_id)
        head=assignment_head(context,contract_id=contract_id)
        effective=effective_assignment(context,contract_id=contract_id)
        result[contract_id]={
            'history':[{
                'record_id':r.id,
                'candidate_id':r.payload.get('candidate_id'),
                'worker_id':r.payload.get('worker_id'),
                'previous_assignment_id':r.payload.get('previous_assignment_id'),
                'reason':r.payload.get('reason'),
            } for r in history],
            'head_assignment_id':head.id if head else None,
            'worker_id':effective.payload.get('worker_id') if effective else None,
        }
    print(json.dumps({'revision':store.get_runtime_revision(),'assignments':result},sort_keys=True))
finally:
    store.close()
"""

_REBUILD = r"""
import json,sys
from aigineering.core.sqlite_store import SQLiteStore
path=sys.argv[1]
store=SQLiteStore(path)
try:
    before=store.runtime_materialization_digest()
    rebuilt=store.rebuild_runtime_materializations(strict=True)
    after=store.runtime_materialization_digest()
    if before!=after or rebuilt!=after:
        raise RuntimeError('runtime materialization digest changed on rebuild')
    print(json.dumps({'before':before,'rebuilt':rebuilt,'after':after},sort_keys=True))
finally:
    store.close()
"""


def _write_exclusive(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_pending_helper():
    """Load the pinned public sibling's pure proofs and bounded RESP proxy."""
    path = Path(__file__).with_name("installed_assignment_priority_pending_smoke.py")
    if _sha256(path.read_bytes()) != _PENDING_HELPER_SHA256:
        raise RuntimeError("shared installed helper differs from reviewed source")
    spec = importlib.util.spec_from_file_location("_installed_assignment_pending", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("shared installed helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    required = ("_FINGERPRINT", "_PROVE_FACT", "_FEED_PROOF", "_relay_proxy")
    if any(not hasattr(module, name) for name in required):
        raise RuntimeError("shared installed helper API is incomplete")
    return module


def _loopback_redis_url(value: str) -> tuple[object, str]:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise RuntimeError("an explicit local plain redis:// URL is required") from None
    if (
        not value
        or parsed.scheme != "redis"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or not parsed.path.lstrip("/").isdigit()
    ):
        raise RuntimeError("an explicit local plain redis:// URL is required")
    return parsed, parsed.path.lstrip("/") or "0"


def _validate_install_origin(aig: Path, prefix: Path, module_path: Path) -> None:
    resolved_prefix = prefix.resolve()
    resolved_module = module_path.resolve()
    if (
        aig.resolve().parent != resolved_prefix / "bin"
        or not resolved_module.is_relative_to(resolved_prefix)
        or "site-packages" not in resolved_module.parts
    ):
        raise RuntimeError("CLI/Python do not identify one installed package")


def _error_code(helper, payload: object) -> str | None:
    return helper._json_error_code(payload)


def _contains_result_ids(value: object) -> bool:
    forbidden = {"assignment_id", "candidate_id", "data", "fact_ids", "record_id"}
    if isinstance(value, dict):
        return bool(forbidden.intersection(value)) or any(
            _contains_result_ids(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_result_ids(item) for item in value)
    return False


def _assert_reassign_batch(
    payload: object,
    *,
    contract_ids: tuple[str, ...],
    previous_by_contract: dict[str, str],
    worker_id: str,
    reason: str,
    base: str | None,
    expected_exit_success: bool,
) -> tuple[dict, list[dict]]:
    if not isinstance(payload, dict) or set(payload) != {"ok", "data", "revision"}:
        raise RuntimeError("reassignment batch envelope is not canonical")
    data = payload["data"]
    if (
        not isinstance(data, dict)
        or (base is not None and data.get("batch_idempotency_key") != base)
        or not isinstance(data.get("batch_idempotency_key"), str)
        or not data["batch_idempotency_key"]
        or type(payload.get("revision")) is not int
        or not isinstance(data.get("results"), list)
        or len(data["results"]) != len(contract_ids)
        or set(previous_by_contract) != set(contract_ids)
    ):
        raise RuntimeError("reassignment batch result data is incomplete")
    rows = data["results"]
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("reassignment result row is malformed")
    if [row.get("contract_id") for row in rows] != list(contract_ids):
        raise RuntimeError("reassignment result order differs from input order")
    outcomes = {
        "accepted": 0,
        "redis_sync_pending": 0,
        "rejected": 0,
        "not_found": 0,
        "outcome_unknown": 0,
        "not_attempted": 0,
    }
    keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("outcome"), str):
            raise RuntimeError("reassignment result row is malformed")
        contract_id = row["contract_id"]
        if (
            row.get("requested_previous_assignment_id")
            != previous_by_contract[contract_id]
        ):
            raise RuntimeError("result row does not echo its exact requested head")
        key = row.get("idempotency_key")
        if not isinstance(key, str) or not key:
            raise RuntimeError("result row omitted its actual target key")
        keys.add(key)
        outcome = row["outcome"]
        if outcome in outcomes:
            outcomes[outcome] += 1
        if outcome in {"accepted", "redis_sync_pending"}:
            operation = row.get("data")
            pending = outcome == "redis_sync_pending"
            if (
                (row.get("ok") is not False if pending else row.get("ok") is not True)
                or not isinstance(operation, dict)
                or operation.get("contract_id") != contract_id
                or operation.get("previous_assignment_id")
                != previous_by_contract[contract_id]
                or operation.get("reason") != reason
                or operation.get("worker_id") != worker_id
                or operation.get("record_type") != "claim.assignment.changed"
                or not isinstance(operation.get("record_id"), str)
                or not operation["record_id"]
                or operation.get("assignment_id") != operation.get("record_id")
                or not isinstance(operation.get("candidate_id"), str)
                or not operation["candidate_id"]
                or operation.get("fact_ids") != [operation.get("record_id")]
                or operation.get("idempotency_key") != key
                or row.get("candidate_id") != operation.get("candidate_id")
                or row.get("fact_ids") != [operation.get("record_id")]
            ):
                raise RuntimeError("reassignment row lost exact accepted result data")
            if pending and (
                row.get("candidate_accepted") is not True
                or row.get("facts_retained") is not True
            ):
                raise RuntimeError("pending assignment row is not marked durable")
        elif outcome in {"rejected", "not_found"}:
            if (
                row.get("ok") is not False
                or row.get("data") is not None
                or "candidate_id" in row
                or "fact_ids" in row
            ):
                raise RuntimeError("known failure row fabricated assignment facts")
        elif outcome == "not_attempted":
            if (
                row.get("ok") is not False
                or row.get("candidate_accepted") is not None
                or row.get("command_attempted") is not False
                or _contains_result_ids(row)
            ):
                raise RuntimeError("not-attempted row contains Candidate/result IDs")
        elif outcome == "outcome_unknown":
            if (
                row.get("ok") is not False
                or row.get("command_attempted") is not True
                or _contains_result_ids(row)
            ):
                raise RuntimeError("unknown row fabricated accepted result metadata")
        else:
            raise RuntimeError("unexpected reassignment result outcome")
    expected_counts = {
        "success_count": outcomes["accepted"],
        "failed_count": len(rows) - outcomes["accepted"],
        "pending_count": outcomes["redis_sync_pending"],
        "unknown_count": outcomes["outcome_unknown"],
        "not_attempted_count": outcomes["not_attempted"],
    }
    if any(data.get(name) != count for name, count in expected_counts.items()):
        raise RuntimeError("batch counts differ from target outcomes")
    if payload["ok"] is not expected_exit_success:
        raise RuntimeError("batch success flag differs from target outcomes")
    return data, rows


def _assert_single_result(
    payload: object,
    *,
    contract_id: str,
    previous: str,
    worker_id: str,
    reason: str,
) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"ok", "data", "revision"}
        or payload.get("ok") is not True
        or type(payload.get("revision")) is not int
        or not isinstance(payload.get("data"), dict)
    ):
        raise RuntimeError("legacy single reassign result envelope changed")
    data = payload["data"]
    if (
        data.get("contract_id") != contract_id
        or data.get("previous_assignment_id") != previous
        or data.get("worker_id") != worker_id
        or data.get("reason") != reason
        or data.get("record_type") != "claim.assignment.changed"
        or data.get("assignment_id") != data.get("record_id")
        or not isinstance(data.get("record_id"), str)
        or not isinstance(data.get("candidate_id"), str)
        or not isinstance(data.get("idempotency_key"), str)
        or data.get("fact_ids") != [data.get("record_id")]
    ):
        raise RuntimeError("legacy single reassign data changed")
    return payload


def _assert_pending_projection(
    before: dict[str, dict],
    after: dict[str, dict],
    *,
    contract_ids: tuple[str, ...],
    first_contract_id: str,
    accepted_data: dict,
) -> None:
    """Keep seeded sibling histories and prove exactly one pending append."""
    if set(before) != set(contract_ids) or set(after) != set(contract_ids):
        raise RuntimeError("pending assignment projection has an incomplete target map")
    if first_contract_id not in contract_ids:
        raise RuntimeError("pending first target is outside the selected batch")
    for contract_id in contract_ids:
        old = before[contract_id]
        new = after[contract_id]
        old_history = old.get("history")
        new_history = new.get("history")
        if (
            not isinstance(old_history, list)
            or not old_history
            or not isinstance(new_history, list)
            or any(not isinstance(item, dict) for item in old_history)
            or any(not isinstance(item, dict) for item in new_history)
            or old.get("head_assignment_id") != old_history[-1].get("record_id")
            or old.get("worker_id") != (old_history[-1].get("worker_id") or None)
        ):
            raise RuntimeError(
                "pending fixture did not start from a consistent seeded history"
            )
        if contract_id != first_contract_id:
            if new != old:
                raise RuntimeError("not-attempted sibling assignment history changed")
            continue
        if (
            len(new_history) != len(old_history) + 1
            or new_history[:-1] != old_history
            or accepted_data.get("contract_id") != first_contract_id
            or old.get("head_assignment_id")
            != accepted_data.get("previous_assignment_id")
            or new.get("head_assignment_id") != accepted_data.get("record_id")
            or new.get("worker_id") != accepted_data.get("worker_id")
        ):
            raise RuntimeError(
                "accepted pending target did not append exactly one new head"
            )
        appended = new_history[-1]
        expected = {
            "record_id": accepted_data.get("record_id"),
            "candidate_id": accepted_data.get("candidate_id"),
            "worker_id": accepted_data.get("worker_id"),
            "previous_assignment_id": accepted_data.get("previous_assignment_id"),
            "reason": accepted_data.get("reason"),
        }
        if appended != expected:
            raise RuntimeError(
                "pending target history append differs from exact accepted data"
            )


def installed_bulk_reassignment_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise exact-head reassignment and clear using the packaged CLI.

    This root-run helper creates signed declarations and assignment heads, but
    no Worker execution or provider call. A bounded local Redis proxy injects
    a sustained post-commit relay failure; all durable proof is captured before
    recovery and exact replay.
    """
    root = Path(cwd) / f"installed-bulk-reassignment-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    workspace = root / "workspace"
    caller = root / "caller"
    evidence = root / "private-evidence"
    for directory in (workspace, caller, evidence):
        directory.mkdir(mode=0o700)
    identity = workspace / ".aig" / "identity"
    database = workspace / ".aig" / "store.db"
    manifest_path = evidence / "manifest.json"
    commands: list[dict] = []
    ids: dict[str, object] = {}
    install_binding: dict[str, str] = {}
    current_phase: str | None = None
    status = "failed"
    error_type: str | None = None
    helper = None
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env.pop("PYTHONHOME", None)
    local_env["PYTHONNOUSERSITE"] = "1"
    for name in tuple(local_env):
        if name in _PROVIDER_KEYS or name.endswith("_API_KEY"):
            local_env.pop(name, None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    local_env["AIGINEERING_REDIS_URL"] = redis_url
    actor_worker = f"worker:reassign-{uuid.uuid4().hex[:12]}"
    prior_worker = f"worker:reassign-prior-{uuid.uuid4().hex[:12]}"
    actor_worker_key = f"worker-key-reassign-{uuid.uuid4().hex[:12]}"
    prior_worker_key = f"worker-key-reassign-prior-{uuid.uuid4().hex[:12]}"
    domain_name = f"bulk-reassignment-{uuid.uuid4().hex}"
    reason_set = "installed bulk set reassignment control"
    reason_clear = "installed bulk clear control"
    reason_later_head = "installed changed-head successor control"
    reason_pending = "installed sustained relay-pending control"
    set_base = f"installed-bulk-reassign-set:{uuid.uuid4().hex}"
    clear_base = f"installed-bulk-reassign-clear:{uuid.uuid4().hex}"

    def run(
        phase: str,
        argv: list[str],
        *,
        expected_exit: int = 0,
        command_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess, object]:
        nonlocal current_phase
        current_phase = phase
        ordinal = len(commands) + 1
        try:
            process = subprocess.Popen(
                argv,
                cwd=caller,
                env=command_env or local_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            stdout = b""
            stderr = b""
            out_path = evidence / f"{ordinal:03d}-{phase}.stdout.raw"
            err_path = evidence / f"{ordinal:03d}-{phase}.stderr.raw"
            _write_exclusive(out_path, stdout)
            _write_exclusive(err_path, stderr)
            commands.append(
                {
                    "phase": phase,
                    "argv": argv,
                    "exit": None,
                    "spawn_error_type": type(exc).__name__,
                    "timed_out": False,
                    "stdout_path": out_path.name,
                    "stderr_path": err_path.name,
                    "stdout_sha256": _sha256(stdout),
                    "stderr_sha256": _sha256(stderr),
                    "expected_exit": expected_exit,
                }
            )
            raise RuntimeError("phase process could not start") from None
        try:
            stdout, stderr = process.communicate(timeout=90)
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
            stdout, stderr = process.communicate(timeout=10)
            stdout = stdout or partial_out
            stderr = stderr or partial_err
            out_path = evidence / f"{ordinal:03d}-{phase}.stdout.raw"
            err_path = evidence / f"{ordinal:03d}-{phase}.stderr.raw"
            _write_exclusive(out_path, stdout)
            _write_exclusive(err_path, stderr)
            commands.append(
                {
                    "phase": phase,
                    "argv": argv,
                    "exit": process.returncode,
                    "timed_out": True,
                    "stdout_path": out_path.name,
                    "stderr_path": err_path.name,
                    "stdout_sha256": _sha256(stdout),
                    "stderr_sha256": _sha256(stderr),
                    "stdout_bytes": len(stdout),
                    "stderr_bytes": len(stderr),
                    "expected_exit": expected_exit,
                }
            )
            raise RuntimeError("phase process timed out") from None
        out_path = evidence / f"{ordinal:03d}-{phase}.stdout.raw"
        err_path = evidence / f"{ordinal:03d}-{phase}.stderr.raw"
        _write_exclusive(out_path, stdout)
        _write_exclusive(err_path, stderr)
        commands.append(
            {
                "phase": phase,
                "argv": argv,
                "exit": process.returncode,
                "timed_out": False,
                "stdout_path": out_path.name,
                "stderr_path": err_path.name,
                "stdout_sha256": _sha256(stdout),
                "stderr_sha256": _sha256(stderr),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
                "expected_exit": expected_exit,
            }
        )
        if process.returncode != expected_exit:
            raise RuntimeError(f"phase {phase} returned unexpected exit")
        try:
            payload = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(f"phase {phase} did not return JSON") from None
        return subprocess.CompletedProcess(
            argv, process.returncode, stdout, stderr
        ), payload

    def cli(phase: str, *args: str, expected_exit: int = 0, command_env=None):
        return run(
            phase,
            [str(aig), "-C", str(workspace), *args, "--json"],
            expected_exit=expected_exit,
            command_env=command_env,
        )

    def fingerprint(phase: str) -> dict:
        _completed, payload = run(
            phase, [str(python), "-c", helper._FINGERPRINT, str(database)]
        )
        if not isinstance(payload, dict) or set(payload.get("tables", {})) != _TABLES:
            raise RuntimeError("phase did not fingerprint all 18 Store tables")
        return payload

    def identity_fingerprint() -> dict[str, str]:
        if not identity.exists():
            return {}
        return {
            item.relative_to(identity).as_posix(): _sha256(item.read_bytes())
            for item in sorted(identity.rglob("*"))
            if item.is_file() and not item.is_symlink()
        }

    def read_assignments(phase: str, contract_ids: tuple[str, ...]) -> dict:
        _completed, payload = run(
            phase,
            [str(python), "-c", _READ_ASSIGNMENTS, str(database), *contract_ids],
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("assignments"), dict)
            or set(payload["assignments"]) != set(contract_ids)
        ):
            raise RuntimeError("read-only assignment projection is incomplete")
        return payload

    def prove_assignment(
        phase: str,
        *,
        contract_id: str,
        record_id: str,
        candidate_id: str,
        previous: str,
        worker_id: str,
    ) -> dict:
        before = fingerprint(f"{phase}-read-before")
        _completed, proof = run(
            phase,
            [
                str(python),
                "-c",
                helper._PROVE_FACT,
                str(database),
                "assignment",
                contract_id,
                record_id,
                candidate_id,
                previous,
                worker_id,
            ],
        )
        after = fingerprint(f"{phase}-read-after")
        if before != after:
            raise RuntimeError("exact assignment proof changed SQLite")
        if (
            not isinstance(proof, dict)
            or proof.get("record_id") != record_id
            or proof.get("candidate_id") != candidate_id
            or proof.get("contract_id") != contract_id
            or proof.get("record_type") != "claim.assignment.changed"
            or proof.get("previous") != previous
            or proof.get("value") != worker_id
            or type(proof.get("fact_revision")) is not int
        ):
            raise RuntimeError("assignment receipt/head/direct fact proof differs")
        return proof

    def check_batch_rows(
        rows: list[dict],
        *,
        contract_ids: tuple[str, ...],
        previous: dict[str, str],
        worker_id: str,
        reason: str,
    ) -> None:
        seen_keys: set[str] = set()
        for row in rows:
            contract_id = row["contract_id"]
            key = row["idempotency_key"]
            if key in seen_keys:
                raise RuntimeError("per-target assignment keys are not unique")
            seen_keys.add(key)
            if row["requested_previous_assignment_id"] != previous[contract_id]:
                raise RuntimeError("row predecessor differs from supplied map")
            if row["outcome"] in {"accepted", "redis_sync_pending"}:
                item = row["data"]
                if item["worker_id"] != worker_id:
                    raise RuntimeError("batch applied a different Worker/clear mode")
                if item["previous_assignment_id"] != previous[contract_id]:
                    raise RuntimeError("signed fact uses a different predecessor")
                if item["reason"] != reason:
                    raise RuntimeError("batch changed the shared reason")
                if row["idempotency_key"] != item["idempotency_key"]:
                    raise RuntimeError("batch row key differs from result key")
            elif row["outcome"] in {"rejected", "not_found", "not_attempted"}:
                if row.get("data") is not None:
                    raise RuntimeError("failure row fabricated operation data")

    def reassign_batch(
        phase: str,
        contract_ids: tuple[str, ...],
        previous: dict[str, str],
        *,
        worker_id: str | None,
        clear: bool,
        reason: str,
        base: str | None,
        expected_exit: int = 0,
        command_env=None,
    ):
        args = ["task", "reassign", *contract_ids]
        if clear:
            args.append("--clear")
        else:
            args.extend(("--worker", worker_id or ""))
        for contract_id in contract_ids:
            args.extend(("--previous-for", f"{contract_id}={previous[contract_id]}"))
        args.extend(("--reason", reason))
        if base is not None:
            args.extend(("--idempotency-key", base))
        _completed, payload = cli(
            phase, *args, expected_exit=expected_exit, command_env=command_env
        )
        operation_worker = "" if clear else (worker_id or "")
        data, rows = _assert_reassign_batch(
            payload,
            contract_ids=contract_ids,
            previous_by_contract=previous,
            worker_id=operation_worker,
            reason=reason,
            base=base,
            expected_exit_success=expected_exit == 0,
        )
        check_batch_rows(
            rows,
            contract_ids=contract_ids,
            previous=previous,
            worker_id=operation_worker,
            reason=reason,
        )
        return payload, data, rows

    def feed_proof(phase: str, record_id: str, after_revision: int) -> dict:
        _completed, payload = run(
            phase,
            [
                str(python),
                "-c",
                helper._FEED_PROOF,
                genesis_id,
                record_id,
                "claim.assignment.changed",
                str(after_revision),
            ],
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("matches"), list
        ):
            raise RuntimeError("native Redis feed proof is malformed")
        return payload

    status = "failed"
    try:
        helper = _load_pending_helper()
        redis_parts, redis_db = _loopback_redis_url(redis_url)
        _completed, origin = run(
            "installed-origin", [str(python), "-c", _INSTALL_ORIGIN]
        )
        prefix = Path(origin["prefix"]).resolve()
        module_path = Path(origin["module"]).resolve()
        _validate_install_origin(Path(aig), prefix, module_path)
        install_binding = {
            "python_prefix": str(prefix),
            "module_path": str(module_path),
            "aig_path": str(Path(aig).resolve()),
            "origin_scope": "same venv/site-packages; caller verifies actual source checkout exclusion",
            "pending_helper_sha256": _sha256(
                Path(__file__)
                .with_name("installed_assignment_priority_pending_smoke.py")
                .read_bytes()
            ),
        }
        identity.mkdir(parents=True, mode=0o700, exist_ok=True)
        _completed, domain = cli(
            "domain-init", "domain", "init", "--domain", domain_name
        )
        if not database.is_file():
            raise RuntimeError("domain init did not create isolated Store")
        genesis_id = domain.get("domain_id") if isinstance(domain, dict) else None
        if not isinstance(genesis_id, str) or not genesis_id:
            raise RuntimeError("domain init omitted immutable Genesis ID")
        ids["domain_name"] = domain_name
        ids["genesis_id"] = genesis_id

        for worker_id, key_id, phase in (
            (actor_worker, actor_worker_key, "worker-replacement"),
            (prior_worker, prior_worker_key, "worker-original"),
        ):
            _completed, public = run(
                f"{phase}-public-key", [str(python), "-c", _WORKER_PUBLIC_KEY]
            )
            if not isinstance(public, dict) or not public.get("public_key"):
                raise RuntimeError("ordinary Worker key generation failed")
            cli(
                f"{phase}-register",
                "worker",
                "register",
                "--worker-id",
                worker_id,
                "--capacity",
                "4",
                "--key-id",
                key_id,
                "--public-key",
                public["public_key"],
            )

        specs = (
            ("set-first", "First set batch target."),
            ("set-stale", "Stale-head set rejection target."),
            ("set-later", "Later set batch target."),
            ("clear-first", "First clear batch target."),
            ("clear-stale", "Stale-head clear rejection target."),
            ("clear-later", "Later clear batch target."),
            ("pending-first", "Accepted-pending first target."),
            ("pending-middle", "Sustained outage middle target."),
            ("pending-last", "Sustained outage final target."),
        )
        contracts: dict[str, str] = {}
        for label, description in specs:
            _completed, created = cli(
                f"task-create-{label}",
                "task",
                "create",
                "--name",
                f"bulk-reassign-{label}-{uuid.uuid4().hex[:8]}",
                "--description",
                description,
                "--budget",
                "3",
            )
            contract_id = (
                created.get("contract_id") if isinstance(created, dict) else None
            )
            if not isinstance(contract_id, str) or not contract_id:
                raise RuntimeError("ordinary task declaration omitted Contract ID")
            contracts[label] = contract_id

        initial_heads: dict[str, str] = {}
        initial_facts: dict[str, dict] = {}
        for label, contract_id in contracts.items():
            _completed, assigned = cli(
                f"initial-assignment-{label}",
                "task",
                "assign",
                contract_id,
                prior_worker,
                "--reason",
                "initial exact-head fixture",
            )
            operation = assigned.get("data") if isinstance(assigned, dict) else None
            if (
                not isinstance(operation, dict)
                or operation.get("contract_id") != contract_id
                or operation.get("worker_id") != prior_worker
                or operation.get("record_type") != "claim.assignment.changed"
                or not isinstance(operation.get("record_id"), str)
                or not isinstance(operation.get("candidate_id"), str)
            ):
                raise RuntimeError("ordinary initial assignment fixture failed")
            proof = prove_assignment(
                f"initial-head-{label}",
                contract_id=contract_id,
                record_id=operation["record_id"],
                candidate_id=operation["candidate_id"],
                previous="",
                worker_id=prior_worker,
            )
            initial_heads[label] = operation["record_id"]
            initial_facts[label] = {
                "record_id": operation["record_id"],
                "candidate_id": operation["candidate_id"],
                "revision": proof["fact_revision"],
            }
        ids["contracts"] = contracts
        ids["workers"] = {"replacement": actor_worker, "original": prior_worker}
        ids["initial_assignment_ids"] = {
            label: item["record_id"] for label, item in initial_facts.items()
        }

        # Global shape rejection and global Redis preflight must be neutral to
        # all SQLite rows and local actor identity files.
        invalid_ids = (
            contracts["set-first"],
            contracts["set-first"],
        )
        invalid_previous = {invalid_ids[0]: initial_heads["set-first"]}
        shape_before = fingerprint("duplicate-shape-before")
        identity_before = identity_fingerprint()
        invalid_args = [
            "task",
            "reassign",
            *invalid_ids,
            "--worker",
            actor_worker,
            "--previous-for",
            f"{invalid_ids[0]}={invalid_previous[invalid_ids[0]]}",
            "--reason",
            "duplicate target validation",
        ]
        _completed, invalid = cli(
            "duplicate-target-shape-no-write",
            *invalid_args,
            expected_exit=1,
        )
        shape_after = fingerprint("duplicate-shape-after")
        identity_after = identity_fingerprint()
        if (
            _error_code(helper, invalid) != "invalid_input"
            or helper._contains_any_id(invalid)
            or shape_before != shape_after
            or identity_before != identity_after
        ):
            raise RuntimeError("invalid batch shape was not a pure no-write")

        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            unavailable_port = port_probe.getsockname()[1]
        unavailable_env = dict(local_env)
        unavailable_env["AIGINEERING_REDIS_URL"] = (
            f"redis://127.0.0.1:{unavailable_port}/{redis_db}"
        )
        preflight_before = fingerprint("redis-preflight-before")
        preflight_identity_before = identity_fingerprint()
        preflight_args = [
            "task",
            "reassign",
            contracts["set-first"],
            contracts["set-later"],
            "--worker",
            actor_worker,
            "--previous-for",
            f"{contracts['set-first']}={initial_heads['set-first']}",
            "--previous-for",
            f"{contracts['set-later']}={initial_heads['set-later']}",
            "--reason",
            "required Redis preflight no-write control",
        ]
        _completed, preflight_error = cli(
            "global-redis-preflight-no-write",
            *preflight_args,
            expected_exit=1,
            command_env=unavailable_env,
        )
        preflight_after = fingerprint("redis-preflight-after")
        preflight_identity_after = identity_fingerprint()
        if (
            _error_code(helper, preflight_error) != "redis_unavailable"
            or helper._contains_any_id(preflight_error)
            or preflight_before != preflight_after
            or preflight_identity_before != preflight_identity_after
        ):
            raise RuntimeError("global Redis preflight fabricated/wrote assignment IDs")

        # Separate homogeneous set and clear batches each have a stale mapped
        # head and a missing Contract between fresh successful siblings.
        set_ids = (
            contracts["set-first"],
            contracts["set-stale"],
            f"contract:missing-reassign-{uuid.uuid4().hex}",
            contracts["set-later"],
        )
        set_previous = {
            contracts["set-first"]: initial_heads["set-first"],
            contracts["set-stale"]: f"assignment:stale-{uuid.uuid4().hex}",
            set_ids[2]: f"assignment:missing-{uuid.uuid4().hex}",
            contracts["set-later"]: initial_heads["set-later"],
        }
        set_before = fingerprint("set-mixed-before")
        set_payload, set_data, set_rows = reassign_batch(
            "set-mixed-known-rejections",
            set_ids,
            set_previous,
            worker_id=actor_worker,
            clear=False,
            reason=reason_set,
            base=set_base,
            expected_exit=1,
        )
        if [row["outcome"] for row in set_rows] != [
            "accepted",
            "rejected",
            "not_found",
            "accepted",
        ]:
            raise RuntimeError("set batch did not continue after typed target failures")
        set_after = fingerprint("set-mixed-after")
        if set_after["revision"] <= set_before["revision"]:
            raise RuntimeError("successful set siblings did not commit")
        set_rows_by_id = {row["contract_id"]: row for row in set_rows}
        for contract_id in (contracts["set-first"], contracts["set-later"]):
            row = set_rows_by_id[contract_id]
            prove_assignment(
                f"set-accepted-{contract_id}",
                contract_id=contract_id,
                record_id=row["data"]["record_id"],
                candidate_id=row["candidate_id"],
                previous=set_previous[contract_id],
                worker_id=actor_worker,
            )

        set_replay_ids = (contracts["set-later"], contracts["set-first"])
        set_replay_previous = {
            contract_id: set_previous[contract_id] for contract_id in set_replay_ids
        }
        set_replay_before = fingerprint("set-reordered-subset-before")
        _set_replay_payload, _set_replay_data, set_replay_rows = reassign_batch(
            "set-reordered-two-member-replay",
            set_replay_ids,
            set_replay_previous,
            worker_id=actor_worker,
            clear=False,
            reason=reason_set,
            base=set_base,
        )
        set_replay_after = fingerprint("set-reordered-subset-after")
        if set_replay_before != set_replay_after:
            raise RuntimeError("set reordered subset replay wrote new facts")
        set_original_keys = {
            row["contract_id"]: row["idempotency_key"]
            for row in set_rows
            if row["outcome"] == "accepted"
        }
        for row in set_replay_rows:
            original = set_rows_by_id[row["contract_id"]]
            if (
                row["idempotency_key"] != set_original_keys[row["contract_id"]]
                or row["data"] != original["data"]
            ):
                raise RuntimeError("set key or result changed under subset replay")

        # A selected member is replayable through the legacy single-target
        # syntax using its returned actual target key.
        set_first_original = set_rows_by_id[contracts["set-first"]]
        set_single_before = fingerprint("set-single-member-replay-before")
        _completed, set_single_replay = cli(
            "set-single-member-actual-key-replay",
            "task",
            "reassign",
            contracts["set-first"],
            "--worker",
            actor_worker,
            "--previous",
            set_previous[contracts["set-first"]],
            "--reason",
            reason_set,
            "--idempotency-key",
            set_first_original["idempotency_key"],
        )
        set_single_after = fingerprint("set-single-member-replay-after")
        _assert_single_result(
            set_single_replay,
            contract_id=contracts["set-first"],
            previous=set_previous[contracts["set-first"]],
            worker_id=actor_worker,
            reason=reason_set,
        )
        if (
            set_single_replay["data"] != set_first_original["data"]
            or set_single_before != set_single_after
        ):
            raise RuntimeError("legacy actual-key member replay changed assignment")

        # A different reason under the same returned key is refused without
        # exposing or committing a fabricated result.
        changed_before = fingerprint("set-changed-intent-before")
        _completed, changed_intent = cli(
            "set-same-key-changed-intent-refused",
            "task",
            "reassign",
            contracts["set-first"],
            "--worker",
            actor_worker,
            "--previous",
            set_previous[contracts["set-first"]],
            "--reason",
            "changed reason must not replay",
            "--idempotency-key",
            set_first_original["idempotency_key"],
            expected_exit=1,
        )
        changed_after = fingerprint("set-changed-intent-after")
        if (
            _error_code(helper, changed_intent) not in {"rejected", "invalid_input"}
            or helper._contains_any_id(changed_intent)
            or changed_before != changed_after
        ):
            raise RuntimeError("changed set intent replayed or wrote a fact")

        # Move a target to a later exact head through the ordinary single owner.
        set_first_initial_head = set_previous[contracts["set-first"]]
        set_first_original_id = set_first_original["data"]["record_id"]
        later_head_reason = reason_later_head
        _completed, later_head_payload = cli(
            "set-first-create-later-head",
            "task",
            "reassign",
            contracts["set-first"],
            "--worker",
            prior_worker,
            "--previous",
            set_first_original_id,
            "--reason",
            later_head_reason,
        )
        later_head_data = _assert_single_result(
            later_head_payload,
            contract_id=contracts["set-first"],
            previous=set_first_original_id,
            worker_id=prior_worker,
            reason=later_head_reason,
        )["data"]
        prove_assignment(
            "set-first-later-head-proof",
            contract_id=contracts["set-first"],
            record_id=later_head_data["record_id"],
            candidate_id=later_head_data["candidate_id"],
            previous=set_first_original_id,
            worker_id=prior_worker,
        )

        # Replaying the old target key after the newer head returns its exact
        # original IDs; a batch with the same base but new head derives a new
        # key and accepts a genuinely new transition.
        original_after_later_before = fingerprint("set-old-key-after-later-head-before")
        _completed, old_key_after_later = cli(
            "set-original-single-key-after-later-head",
            "task",
            "reassign",
            contracts["set-first"],
            "--worker",
            actor_worker,
            "--previous",
            set_first_initial_head,
            "--reason",
            reason_set,
            "--idempotency-key",
            set_first_original["idempotency_key"],
        )
        original_after_later_after = fingerprint("set-old-key-after-later-head-after")
        _assert_single_result(
            old_key_after_later,
            contract_id=contracts["set-first"],
            previous=set_first_initial_head,
            worker_id=actor_worker,
            reason=reason_set,
        )
        if (
            old_key_after_later["data"] != set_first_original["data"]
            or original_after_later_before != original_after_later_after
        ):
            raise RuntimeError("exact old set replay changed after later head")
        changed_head_ids = (contracts["set-first"], contracts["set-later"])
        changed_head_previous = {
            contracts["set-first"]: later_head_data["record_id"],
            contracts["set-later"]: set_previous[contracts["set-later"]],
        }
        changed_head_before = fingerprint("set-changed-head-same-base-before")
        _changed_head_payload, _changed_head_data, changed_head_rows = reassign_batch(
            "set-same-base-changed-head",
            changed_head_ids,
            changed_head_previous,
            worker_id=actor_worker,
            clear=False,
            reason=reason_set,
            base=set_base,
        )
        changed_head_after = fingerprint("set-changed-head-same-base-after")
        if changed_head_after["revision"] <= changed_head_before["revision"]:
            raise RuntimeError("changed exact head did not produce a fresh assignment")
        changed_first = changed_head_rows[0]
        if (
            changed_first["outcome"] != "accepted"
            or changed_first["idempotency_key"] == set_first_original["idempotency_key"]
            or changed_first["data"]["record_id"] == set_first_original_id
            or changed_first["data"]["previous_assignment_id"]
            != later_head_data["record_id"]
        ):
            raise RuntimeError("changed head reused the original signed transition")
        if (
            changed_head_rows[1]["idempotency_key"]
            != set_rows_by_id[contracts["set-later"]]["idempotency_key"]
            or changed_head_rows[1]["data"]
            != set_rows_by_id[contracts["set-later"]]["data"]
        ):
            raise RuntimeError("same-base unchanged sibling did not exact-replay")
        prove_assignment(
            "set-changed-head-new-proof",
            contract_id=contracts["set-first"],
            record_id=changed_first["data"]["record_id"],
            candidate_id=changed_first["candidate_id"],
            previous=later_head_data["record_id"],
            worker_id=actor_worker,
        )

        # Clear is a separate homogeneous batch with its own mixed known
        # failures and exact predecessor map.
        clear_missing = f"contract:missing-clear-{uuid.uuid4().hex}"
        clear_ids = (
            contracts["clear-first"],
            contracts["clear-stale"],
            clear_missing,
            contracts["clear-later"],
        )
        clear_previous = {
            contracts["clear-first"]: initial_heads["clear-first"],
            contracts["clear-stale"]: f"assignment:clear-stale-{uuid.uuid4().hex}",
            clear_missing: f"assignment:clear-missing-{uuid.uuid4().hex}",
            contracts["clear-later"]: initial_heads["clear-later"],
        }
        clear_before = fingerprint("clear-mixed-before")
        clear_payload, clear_data, clear_rows = reassign_batch(
            "clear-mixed-known-rejections",
            clear_ids,
            clear_previous,
            worker_id=None,
            clear=True,
            reason=reason_clear,
            base=clear_base,
            expected_exit=1,
        )
        if [row["outcome"] for row in clear_rows] != [
            "accepted",
            "rejected",
            "not_found",
            "accepted",
        ]:
            raise RuntimeError("clear batch did not continue after typed failures")
        clear_after = fingerprint("clear-mixed-after")
        if clear_after["revision"] <= clear_before["revision"]:
            raise RuntimeError("successful clear siblings did not commit")
        clear_by_id = {row["contract_id"]: row for row in clear_rows}
        clear_first_row = clear_by_id[contracts["clear-first"]]
        clear_last_row = clear_by_id[contracts["clear-later"]]
        for row in (clear_first_row, clear_last_row):
            prove_assignment(
                "clear-exact-fact-proof",
                contract_id=row["contract_id"],
                record_id=row["data"]["record_id"],
                candidate_id=row["candidate_id"],
                previous=clear_previous[row["contract_id"]],
                worker_id="",
            )
        clear_projection = read_assignments(
            "clear-head-remains-in-history",
            (contracts["clear-first"], contracts["clear-later"]),
        )["assignments"]
        for contract_id, row in (
            (contracts["clear-first"], clear_first_row),
            (contracts["clear-later"], clear_last_row),
        ):
            projection = clear_projection[contract_id]
            if (
                projection["head_assignment_id"] != row["data"]["record_id"]
                or projection["worker_id"] is not None
                or projection["history"][-1]["worker_id"] != ""
            ):
                raise RuntimeError("clear fact did not remain the assignment head")

        # Set after a clear names the nonempty clear fact as predecessor.
        clear_first_clear_id = clear_first_row["data"]["record_id"]
        _completed, set_after_clear_payload = cli(
            "set-after-clear-head",
            "task",
            "reassign",
            contracts["clear-first"],
            "--worker",
            actor_worker,
            "--previous",
            clear_first_clear_id,
            "--reason",
            "set after exact clear head",
        )
        set_after_clear = _assert_single_result(
            set_after_clear_payload,
            contract_id=contracts["clear-first"],
            previous=clear_first_clear_id,
            worker_id=actor_worker,
            reason="set after exact clear head",
        )["data"]
        prove_assignment(
            "set-after-clear-exact-proof",
            contract_id=contracts["clear-first"],
            record_id=set_after_clear["record_id"],
            candidate_id=set_after_clear["candidate_id"],
            previous=clear_first_clear_id,
            worker_id=actor_worker,
        )

        # Exact clear replay after a later set still returns the old clear fact.
        clear_replay_ids = (contracts["clear-later"], contracts["clear-first"])
        clear_replay_previous = {cid: clear_previous[cid] for cid in clear_replay_ids}
        clear_replay_before = fingerprint("clear-old-head-replay-before")
        _clear_replay_payload, _clear_replay_data, clear_replay_rows = reassign_batch(
            "clear-old-base-after-later-set",
            clear_replay_ids,
            clear_replay_previous,
            worker_id=None,
            clear=True,
            reason=reason_clear,
            base=clear_base,
        )
        clear_replay_after = fingerprint("clear-old-head-replay-after")
        if clear_replay_before != clear_replay_after:
            raise RuntimeError("clear replay after later set wrote another fact")
        for row in clear_replay_rows:
            if row["data"] != clear_by_id[row["contract_id"]]["data"]:
                raise RuntimeError("clear replay after later set changed IDs")

        # Sustained event-specific proxy fault: first transition is durable,
        # later callbacks fail pre-sync without publishing their targets.
        pending_ids = (
            contracts["pending-first"],
            contracts["pending-middle"],
            contracts["pending-last"],
        )
        pending_previous = {
            contract_id: initial_heads[label]
            for label, contract_id in (
                ("pending-first", contracts["pending-first"]),
                ("pending-middle", contracts["pending-middle"]),
                ("pending-last", contracts["pending-last"]),
            )
        }
        pending_base = None
        pending_before = fingerprint("pending-batch-before")
        pending_projection_before = read_assignments(
            "pending-seeded-projection-before-relay-fault", pending_ids
        )["assignments"]
        server, server_thread, proxy_url = helper._relay_proxy(redis_url)
        proxy_env = dict(local_env)
        proxy_env["AIGINEERING_REDIS_URL"] = proxy_url
        pending_args = ["task", "reassign", *pending_ids, "--worker", actor_worker]
        for contract_id in pending_ids:
            pending_args.extend(
                ("--previous-for", f"{contract_id}={pending_previous[contract_id]}")
            )
        pending_args.extend(("--reason", reason_pending))
        try:
            _completed, pending_payload = cli(
                "pending-first-relay-fault",
                *pending_args,
                expected_exit=1,
                command_env=proxy_env,
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        if not server.faults or any(
            record_type != "claim.assignment.changed" for record_type in server.faults
        ):
            raise RuntimeError("proxy did not refuse only assignment relay appends")
        pending_data, pending_rows = _assert_reassign_batch(
            pending_payload,
            contract_ids=pending_ids,
            previous_by_contract=pending_previous,
            worker_id=actor_worker,
            reason=reason_pending,
            base=None,
            expected_exit_success=False,
        )
        pending_base = pending_data["batch_idempotency_key"]
        if [row["outcome"] for row in pending_rows] != [
            "redis_sync_pending",
            "not_attempted",
            "not_attempted",
        ]:
            raise RuntimeError("sustained outage did not retain exact pending/tails")
        pending_first = pending_rows[0]
        pending_durable = fingerprint("pending-first-durable-before-recovery")
        if pending_durable["revision"] <= pending_before["revision"]:
            raise RuntimeError("accepted-pending assignment was not durable")
        pending_proof = prove_assignment(
            "pending-first-exact-proof-before-recovery",
            contract_id=pending_ids[0],
            record_id=pending_first["data"]["record_id"],
            candidate_id=pending_first["candidate_id"],
            previous=pending_previous[pending_ids[0]],
            worker_id=actor_worker,
        )
        pending_projection = read_assignments(
            "pending-projection-before-recovery", pending_ids
        )["assignments"]
        _assert_pending_projection(
            pending_projection_before,
            pending_projection,
            contract_ids=pending_ids,
            first_contract_id=pending_ids[0],
            accepted_data=pending_first["data"],
        )
        native_absent = feed_proof(
            "pending-native-feed-absent-before-recovery",
            pending_first["data"]["record_id"],
            pending_before["revision"],
        )
        if native_absent["matches"]:
            raise RuntimeError("pending assignment reached Redis before recovery")

        # Proxy is already stopped. The singleton actual key recovers the exact
        # original Candidate and feed event without adding SQLite history.
        pending_actual_key = pending_first["idempotency_key"]
        pending_replay_before = fingerprint("pending-singleton-replay-before")
        _completed, pending_replay_payload = cli(
            "pending-first-singleton-actual-key-replay",
            "task",
            "reassign",
            pending_ids[0],
            "--worker",
            actor_worker,
            "--previous",
            pending_previous[pending_ids[0]],
            "--reason",
            reason_pending,
            "--idempotency-key",
            pending_actual_key,
        )
        pending_replay_after = fingerprint("pending-singleton-replay-after")
        pending_replay = _assert_single_result(
            pending_replay_payload,
            contract_id=pending_ids[0],
            previous=pending_previous[pending_ids[0]],
            worker_id=actor_worker,
            reason=reason_pending,
        )
        if (
            pending_replay["data"] != pending_first["data"]
            or pending_replay_before != pending_replay_after
        ):
            raise RuntimeError("pending singleton recovery changed original IDs")
        pending_feed_after = feed_proof(
            "pending-native-feed-after-singleton-recovery",
            pending_first["data"]["record_id"],
            pending_before["revision"],
        )
        if (
            len(pending_feed_after["matches"]) != 1
            or pending_feed_after["matches"][0].get("record_id")
            != pending_first["data"]["record_id"]
            or pending_feed_after["matches"][0].get("revision")
            != pending_first["data"]["revision"]
        ):
            raise RuntimeError("pending singleton replay did not catch up its feed")

        # After relay recovery, untouched siblings can be accepted with the
        # original batch base. Full batch replay must preserve all original IDs.
        later_ids = (pending_ids[2], pending_ids[1])
        later_previous = {cid: pending_previous[cid] for cid in later_ids}
        later_before = fingerprint("pending-later-siblings-before")
        _later_payload, later_data, later_rows = reassign_batch(
            "pending-later-siblings-same-base",
            later_ids,
            later_previous,
            worker_id=actor_worker,
            clear=False,
            reason=reason_pending,
            base=pending_base,
        )
        later_after = fingerprint("pending-later-siblings-after")
        if later_after["revision"] <= later_before["revision"]:
            raise RuntimeError("post-recovery later assignments did not commit")
        pending_by_id = {row["contract_id"]: row for row in pending_rows}
        for row in later_rows:
            if (
                row["idempotency_key"]
                != pending_by_id[row["contract_id"]]["idempotency_key"]
            ):
                raise RuntimeError("post-recovery target key changed")
        all_pending_originals = (pending_first, *later_rows)
        full_ids = (pending_ids[2], pending_ids[0], pending_ids[1])
        full_previous = {cid: pending_previous[cid] for cid in full_ids}
        full_before = fingerprint("pending-full-reordered-replay-before")
        _full_payload, _full_data, full_rows = reassign_batch(
            "pending-full-reordered-batch-replay",
            full_ids,
            full_previous,
            worker_id=actor_worker,
            clear=False,
            reason=reason_pending,
            base=pending_base,
        )
        full_after = fingerprint("pending-full-reordered-replay-after")
        if full_before != full_after:
            raise RuntimeError("full pending batch replay wrote duplicate history")
        expected_pending_ids = {
            row["contract_id"]: row["data"]["record_id"]
            for row in all_pending_originals
        }
        if {
            row["contract_id"]: row["data"]["record_id"] for row in full_rows
        } != expected_pending_ids:
            raise RuntimeError("full replay changed a pending batch member fact ID")
        for original in all_pending_originals:
            proof = prove_assignment(
                "pending-each-member-final-proof",
                contract_id=original["contract_id"],
                record_id=original["data"]["record_id"],
                candidate_id=original["candidate_id"],
                previous=pending_previous[original["contract_id"]],
                worker_id=actor_worker,
            )
            member_feed = feed_proof(
                f"pending-native-feed-member-{original['contract_id']}",
                original["data"]["record_id"],
                pending_before["revision"],
            )
            if (
                proof["record_id"] != original["data"]["record_id"]
                or len(member_feed["matches"]) != 1
                or member_feed["matches"][0].get("record_id")
                != original["data"]["record_id"]
                or member_feed["matches"][0].get("revision")
                != original["data"]["revision"]
            ):
                raise RuntimeError(
                    "a recovered batch member lacks exact Redis feed proof"
                )

        final_ids = tuple(contracts.values())
        projection_before_rebuild = read_assignments(
            "assignment-projection-before-rebuild", final_ids
        )
        rebuild_before = fingerprint("rebuild-before")
        _completed, rebuild_result = run(
            "runtime-materialization-rebuild",
            [str(python), "-c", _REBUILD, str(database)],
        )
        rebuild_after = fingerprint("rebuild-after")
        projection_after_rebuild = read_assignments(
            "assignment-projection-after-rebuild", final_ids
        )
        if (
            projection_before_rebuild["assignments"]
            != projection_after_rebuild["assignments"]
            or rebuild_result.get("before") != rebuild_result.get("after")
            or rebuild_before["tables"]["runtime_records"]
            != rebuild_after["tables"]["runtime_records"]
        ):
            raise RuntimeError("reopen/rebuild changed assignment history semantics")

        status = "complete"
        ids.update(
            {
                "set_batch_base": set_base,
                "set_target_fact_ids": {
                    cid: row["data"]["record_id"]
                    for cid, row in set_rows_by_id.items()
                    if row["outcome"] == "accepted"
                },
                "clear_batch_base": clear_base,
                "clear_fact_ids": [
                    clear_first_row["data"]["record_id"],
                    clear_last_row["data"]["record_id"],
                ],
                "clear_then_set_fact_id": set_after_clear["record_id"],
                "set_later_head_id": later_head_data["record_id"],
                "set_changed_head_id": changed_first["data"]["record_id"],
                "pending_batch_base": pending_base,
                "pending_fact_ids": [
                    row["data"]["record_id"] for row in all_pending_originals
                ],
                "pending_candidate_ids": [
                    row["candidate_id"] for row in all_pending_originals
                ],
                "relay_fault_attempt_count": len(server.faults),
                "pending_feed_before_recovery": len(native_absent["matches"]),
                "pending_feed_after_recovery": len(pending_feed_after["matches"]),
                "sqlite_table_count": len(rebuild_after["tables"]),
                "final_revision": rebuild_after["revision"],
                "worker_invocations": 0,
                "provider_calls": 0,
                "source_only_controls_not_run": [
                    "64-target actual signed batch",
                    "malformed UTF-8/surrogate ID/head",
                    "plain ValueError/KeyError/TypeError unknown-stop injection",
                    "current operator key revocation and stale-head race injection",
                ],
            }
        )
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        manifest = {
            "schema": "aigineering.installed-bulk-reassignment.v1",
            "status": status,
            "current_phase": current_phase,
            "helper_sha256": _sha256(Path(__file__).read_bytes()),
            "workspace": str(workspace),
            "database": str(database),
            "domain_name": domain_name,
            "redis_database": redis_db if "redis_db" in locals() else None,
            "provider_invoked": False,
            "worker_invoked": False,
            "provider_credentials_in_child_env": False,
            "source_binding": "same venv/site-packages checked here; root wrapper checks exclusion of actual source checkout",
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
        raise RuntimeError("installed bulk reassignment control did not complete")
    return {
        "status": status,
        "evidence_dir": str(evidence),
        "domain_name": domain_name,
        "contract_count": len(contracts),
        "fact_count": len(ids["set_target_fact_ids"])
        + 1
        + 1
        + len(ids["clear_fact_ids"])
        + 1
        + len(ids["pending_fact_ids"]),
        "worker_invocations": 0,
        "provider_calls": 0,
    }
