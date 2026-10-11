"""Source-external acceptance controls for signed bulk initial assignment.

The root acceptance harness calls :func:`installed_bulk_assignment_smoke` with
the packaged CLI and interpreter. This helper creates an isolated ordinary
domain and registered idle Workers, but never invokes a Worker or provider.
Raw command streams and durable evidence stay in a private evidence directory;
the returned value contains only safe IDs and phase summaries.
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

_PENDING_HELPER_SHA256 = (
    "665d4d6dbe8bf57ae941cc26a777441a8411aee6212049b31223ca8d02482a4f"
)

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
    "prefix": str(pathlib.Path(sys.prefix).absolute()),
    "module": str(pathlib.Path(aigineering.__file__).absolute()),
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
    """Load the public sibling helper without depending on source modules."""
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
        or (port is not None and not 1 <= port <= 65535)
        or parsed.query
        or parsed.fragment
        or (parsed.path and not parsed.path.lstrip("/").isdigit())
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


def _contains_assignment_result_metadata(value: object) -> bool:
    forbidden = {"assignment_id", "candidate_id", "data", "fact_ids", "record_id"}
    if isinstance(value, dict):
        return bool(forbidden.intersection(value)) or any(
            _contains_assignment_result_metadata(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_assignment_result_metadata(item) for item in value)
    return False


def _assert_batch(
    payload: object,
    *,
    contract_ids: tuple[str, ...],
    worker_id: str,
    reason: str,
    base: str | None = None,
    expected_exit_success: bool,
) -> tuple[dict, list[dict]]:
    if not isinstance(payload, dict) or set(payload) != {"ok", "data", "revision"}:
        raise RuntimeError("batch result envelope is not canonical")
    data = payload["data"]
    if (
        not isinstance(data, dict)
        or data.get("batch_idempotency_key") != base
        and base is not None
        or not isinstance(data.get("batch_idempotency_key"), str)
        or not data["batch_idempotency_key"]
        or type(payload.get("revision")) is not int
        or not isinstance(data.get("results"), list)
        or len(data["results"]) != len(contract_ids)
    ):
        raise RuntimeError("batch result data is incomplete")
    rows = data["results"]
    if [row.get("contract_id") for row in rows] != list(contract_ids):
        raise RuntimeError("batch result order differs from input order")
    counts = {
        "accepted": 0,
        "redis_sync_pending": 0,
        "rejected": 0,
        "not_found": 0,
        "outcome_unknown": 0,
        "not_attempted": 0,
    }
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("outcome"), str):
            raise RuntimeError("batch result row is malformed")
        if row["outcome"] == "accepted":
            counts["accepted"] += 1
            operation = row.get("data")
            if (
                row.get("ok") is not True
                or not isinstance(operation, dict)
                or operation.get("contract_id") != row["contract_id"]
                or operation.get("worker_id") != worker_id
                or operation.get("reason") != reason
                or operation.get("previous_assignment_id") != ""
                or operation.get("record_type") != "claim.assignment.changed"
                or not isinstance(operation.get("record_id"), str)
                or operation.get("assignment_id") != operation.get("record_id")
                or not isinstance(operation.get("candidate_id"), str)
                or not isinstance(operation.get("idempotency_key"), str)
                or operation.get("fact_ids") != [operation.get("record_id")]
                or row.get("fact_ids") != [operation.get("record_id")]
                or row.get("candidate_id") != operation.get("candidate_id")
                or row.get("idempotency_key") != operation.get("idempotency_key")
            ):
                raise RuntimeError("accepted row does not bind exact assignment result")
        elif row["outcome"] == "redis_sync_pending":
            counts["redis_sync_pending"] += 1
            operation = row.get("data")
            if (
                row.get("ok") is not False
                or row.get("outcome") != "redis_sync_pending"
                or row.get("candidate_accepted") is not True
                or row.get("facts_retained") is not True
                or not isinstance(operation, dict)
                or operation.get("contract_id") != row["contract_id"]
                or operation.get("worker_id") != worker_id
                or operation.get("reason") != reason
                or operation.get("previous_assignment_id") != ""
                or operation.get("record_type") != "claim.assignment.changed"
                or operation.get("assignment_id") != operation.get("record_id")
                or not isinstance(operation.get("idempotency_key"), str)
                or row.get("idempotency_key") != operation.get("idempotency_key")
                or row.get("fact_ids") != [operation.get("record_id")]
                or row.get("candidate_id") != operation.get("candidate_id")
            ):
                raise RuntimeError("accepted-pending row lacks durable assignment IDs")
        elif row["outcome"] == "not_attempted":
            counts["not_attempted"] += 1
            if (
                row.get("ok") is not False
                or row.get("command_attempted") is not False
                or "candidate_accepted" not in row
                or row.get("candidate_accepted") is not None
                or row.get("facts_retained") is True
                or _contains_assignment_result_metadata(row)
            ):
                raise RuntimeError(
                    "not-attempted row claims a Candidate or assignment result"
                )
        elif row["outcome"] == "rejected":
            counts["rejected"] += 1
            if row.get("ok") is not False or row.get("data") is not None:
                raise RuntimeError("rejection row fabricated an assignment result")
        elif row["outcome"] == "not_found":
            counts["not_found"] += 1
            if row.get("ok") is not False or row.get("data") is not None:
                raise RuntimeError("rejection row fabricated an assignment result")
        elif row["outcome"] == "outcome_unknown":
            counts["outcome_unknown"] += 1
            if row.get("ok") is not False or row.get("command_attempted") is not True:
                raise RuntimeError("unknown result lost the attempted command boundary")
        else:
            raise RuntimeError("unexpected target outcome in assignment batch")
    expected_counts = {
        "success_count": counts["accepted"],
        "failed_count": len(rows) - counts["accepted"],
        "pending_count": counts["redis_sync_pending"],
        "unknown_count": counts["outcome_unknown"],
        "not_attempted_count": counts["not_attempted"],
    }
    if any(data.get(key) != value for key, value in expected_counts.items()):
        raise RuntimeError("batch counters differ from row outcomes")
    if payload["ok"] is not expected_exit_success:
        raise RuntimeError("batch envelope success flag differs from target outcomes")
    return data, rows


def installed_bulk_assignment_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise packaged bulk assignment using an isolated local Redis domain.

    This is a root-run acceptance helper. It uses only normal packaged CLI
    setup/publication commands plus read-only installed-Python proof queries.
    No Worker or provider is invoked. The accepted-pending phase uses the
    shared sibling helper's bounded loopback RESP proxy, and records its raw
    command evidence under a fresh private directory.
    """
    helper = _load_pending_helper()
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    redis_parts, redis_db = _loopback_redis_url(redis_url)
    root = Path(cwd) / f"installed-bulk-assignment-{uuid.uuid4().hex}"
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
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env.pop("PYTHONHOME", None)
    local_env["PYTHONNOUSERSITE"] = "1"
    for name in tuple(local_env):
        if name in _PROVIDER_KEYS or name.endswith("_API_KEY"):
            local_env.pop(name, None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")
    local_env["AIGINEERING_REDIS_URL"] = redis_url
    actor_worker = f"worker:bulk-assignment-{uuid.uuid4().hex[:12]}"
    prior_worker = f"worker:bulk-prior-{uuid.uuid4().hex[:12]}"
    actor_worker_key = f"worker-key-bulk-{uuid.uuid4().hex[:12]}"
    prior_worker_key = f"worker-key-prior-{uuid.uuid4().hex[:12]}"
    domain_name = f"bulk-assignment-{uuid.uuid4().hex}"
    reason_single = "legacy single assignment control"
    reason_mixed = "explicit-base partial assignment control"
    reason_pending = "generated-base pending assignment control"
    explicit_base = f"installed-bulk-assignment:{uuid.uuid4().hex}"

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
            helper._write_exclusive(out_path, stdout)
            helper._write_exclusive(err_path, stderr)
            commands.append(
                {
                    "phase": phase,
                    "argv": argv,
                    "exit": None,
                    "spawn_error_type": type(exc).__name__,
                    "timed_out": False,
                    "stdout_path": out_path.name,
                    "stderr_path": err_path.name,
                    "stdout_sha256": helper._digest(stdout),
                    "stderr_sha256": helper._digest(stderr),
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
            helper._write_exclusive(out_path, stdout)
            helper._write_exclusive(err_path, stderr)
            commands.append(
                {
                    "phase": phase,
                    "argv": argv,
                    "exit": process.returncode,
                    "timed_out": True,
                    "stdout_path": out_path.name,
                    "stderr_path": err_path.name,
                    "stdout_sha256": helper._digest(stdout),
                    "stderr_sha256": helper._digest(stderr),
                    "stdout_bytes": len(stdout),
                    "stderr_bytes": len(stderr),
                    "expected_exit": expected_exit,
                }
            )
            raise RuntimeError("phase process timed out") from None
        out_path = evidence / f"{ordinal:03d}-{phase}.stdout.raw"
        err_path = evidence / f"{ordinal:03d}-{phase}.stderr.raw"
        helper._write_exclusive(out_path, stdout)
        helper._write_exclusive(err_path, stderr)
        commands.append(
            {
                "phase": phase,
                "argv": argv,
                "exit": process.returncode,
                "timed_out": False,
                "stdout_path": out_path.name,
                "stderr_path": err_path.name,
                "stdout_sha256": helper._digest(stdout),
                "stderr_sha256": helper._digest(stderr),
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

    def prove_row(phase: str, row: dict) -> dict:
        operation = row["data"]
        before = fingerprint(f"{phase}-read-before")
        _completed, proof = run(
            phase,
            [
                str(python),
                "-c",
                helper._PROVE_FACT,
                str(database),
                "assignment",
                operation["contract_id"],
                operation["record_id"],
                operation["candidate_id"],
                "",
                operation["worker_id"],
            ],
        )
        after = fingerprint(f"{phase}-read-after")
        if before != after:
            raise RuntimeError("durable assignment proof changed SQLite")
        if (
            not isinstance(proof, dict)
            or proof.get("candidate_id") != operation["candidate_id"]
            or proof.get("record_id") != operation["record_id"]
            or proof.get("record_type") != "claim.assignment.changed"
            or proof.get("contract_id") != operation["contract_id"]
            or proof.get("previous") != ""
            or proof.get("value") != operation["worker_id"]
            or proof.get("fact_revision") != operation.get("revision")
        ):
            raise RuntimeError("exact assignment receipt/head/fact proof differs")
        return proof

    def relay_fault_batch(phase: str, command: list[str], *, expected_exit: int):
        server, server_thread, proxy_url = helper._relay_proxy(redis_url)
        proxy_env = dict(local_env)
        proxy_env["AIGINEERING_REDIS_URL"] = proxy_url
        try:
            completed, payload = run(
                phase,
                [str(aig), "-C", str(workspace), *command, "--json"],
                expected_exit=expected_exit,
                command_env=proxy_env,
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        if not server.faults or any(
            record_type != "claim.assignment.changed" for record_type in server.faults
        ):
            raise RuntimeError("relay proxy did not isolate assignment append only")
        return completed, payload, tuple(server.faults)

    status = "failed"
    try:
        _completed, origin = run(
            "installed-origin",
            [str(python), "-c", _INSTALL_ORIGIN],
        )
        prefix = Path(origin["prefix"]).resolve()
        module_path = Path(origin["module"]).resolve()
        _validate_install_origin(Path(aig), prefix, module_path)
        install_binding = {
            "python_prefix": str(prefix),
            "module_path": str(module_path),
            "aig_path": str(Path(aig).resolve()),
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
            (actor_worker, actor_worker_key, "worker-one"),
            (prior_worker, prior_worker_key, "worker-prior"),
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
                "2",
                "--key-id",
                key_id,
                "--public-key",
                public["public_key"],
            )

        task_specs = (
            ("single", "Legacy single assignment control."),
            ("mixed-first", "First partial-batch assignment target."),
            ("mixed-later", "Later partial-batch assignment target."),
            ("pending-first", "First accepted-pending target."),
            ("pending-middle", "Middle accepted-pending target."),
            ("pending-last", "Last accepted-pending target."),
        )
        contracts: dict[str, str] = {}
        for label, description in task_specs:
            _completed, created = cli(
                f"task-create-{label}",
                "task",
                "create",
                "--name",
                f"bulk-assignment-{label}-{uuid.uuid4().hex[:8]}",
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
        ids["contracts"] = contracts
        ids["worker_id"] = actor_worker
        ids["prior_worker_id"] = prior_worker

        # Pure input validation must happen before opening Store or contacting
        # Redis. Duplicate IDs give a no-write control on the installed CLI.
        invalid_before = fingerprint("invalid-shape-before")
        invalid_identity_before = identity_fingerprint()
        _completed, invalid = cli(
            "invalid-duplicate-contracts-no-write",
            "task",
            "assign",
            contracts["mixed-first"],
            contracts["mixed-first"],
            actor_worker,
            "--reason",
            "duplicate validation control",
            expected_exit=1,
        )
        invalid_after = fingerprint("invalid-shape-after")
        invalid_identity_after = identity_fingerprint()
        if (
            _error_code(helper, invalid) != "invalid_input"
            or helper._contains_any_id(invalid)
            or invalid_before != invalid_after
            or invalid_identity_before != invalid_identity_after
        ):
            raise RuntimeError("invalid shape was not rejected as a pure no-write")

        # Required Redis preflight failure must not create an assignment
        # Candidate. Use a just-released local port; no native Redis state is
        # changed by this expected-failure control.
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            unavailable_port = port_probe.getsockname()[1]
        unavailable_env = dict(local_env)
        unavailable_env["AIGINEERING_REDIS_URL"] = (
            f"redis://127.0.0.1:{unavailable_port}/{redis_db}"
        )
        preflight_before = fingerprint("redis-preflight-before")
        preflight_identity_before = identity_fingerprint()
        _completed, unavailable = cli(
            "required-redis-preflight-no-write",
            "task",
            "assign",
            contracts["mixed-first"],
            contracts["mixed-later"],
            actor_worker,
            "--reason",
            "required Redis preflight control",
            command_env=unavailable_env,
            expected_exit=1,
        )
        preflight_after = fingerprint("redis-preflight-after")
        preflight_identity_after = identity_fingerprint()
        if (
            _error_code(helper, unavailable) != "redis_unavailable"
            or helper._contains_any_id(unavailable)
            or preflight_before != preflight_after
            or preflight_identity_before != preflight_identity_after
        ):
            raise RuntimeError("global Redis preflight fabricated/wrote a result")

        # Keep the exact old single command path available.
        single_before = fingerprint("legacy-single-before")
        _completed, single = cli(
            "legacy-single-assignment",
            "task",
            "assign",
            contracts["single"],
            prior_worker,
            "--reason",
            reason_single,
        )
        single_data = single.get("data") if isinstance(single, dict) else None
        if (
            not isinstance(single, dict)
            or set(single) != {"ok", "data", "revision"}
            or single.get("ok") is not True
            or not isinstance(single_data, dict)
            or single_data.get("contract_id") != contracts["single"]
            or single_data.get("worker_id") != prior_worker
            or single_data.get("previous_assignment_id") != ""
            or single_data.get("reason") != reason_single
            or single_data.get("record_type") != "claim.assignment.changed"
            or single_data.get("assignment_id") != single_data.get("record_id")
            or not str(single_data.get("idempotency_key", "")).startswith(
                "task-assignment:"
            )
            or single_data.get("fact_ids") != [single_data.get("record_id")]
        ):
            raise RuntimeError("legacy single assignment contract changed")
        single_after = fingerprint("legacy-single-after")
        if single_before["revision"] >= single_after["revision"]:
            raise RuntimeError("legacy single assignment did not commit a new fact")
        prove_row(
            "legacy-single-exact-proof",
            {"data": single_data},
        )
        ids["single_fact_id"] = single_data["record_id"]
        ids["single_candidate_id"] = single_data["candidate_id"]

        # A successful and an assigned-middle/not-found partial batch exercise
        # explicit base preservation, input order, and continued known errors.
        mixed_ids = (
            contracts["mixed-first"],
            contracts["single"],
            f"contract:missing-{uuid.uuid4().hex}",
            contracts["mixed-later"],
        )
        mixed_before = fingerprint("mixed-explicit-base-before")
        _completed, mixed_payload = cli(
            "mixed-explicit-base-partial-batch",
            "task",
            "assign",
            *mixed_ids,
            actor_worker,
            "--reason",
            reason_mixed,
            "--idempotency-key",
            explicit_base,
            expected_exit=1,
        )
        mixed_data, mixed_rows = _assert_batch(
            mixed_payload,
            contract_ids=mixed_ids,
            worker_id=actor_worker,
            reason=reason_mixed,
            base=explicit_base,
            expected_exit_success=False,
        )
        if [row["outcome"] for row in mixed_rows] != [
            "accepted",
            "rejected",
            "not_found",
            "accepted",
        ]:
            raise RuntimeError("known middle assignment failures did not continue")
        mixed_after = fingerprint("mixed-explicit-base-after")
        if mixed_after["revision"] <= mixed_before["revision"]:
            raise RuntimeError("partial batch did not retain successful siblings")
        mixed_by_contract = {row["contract_id"]: row for row in mixed_rows}
        for label in ("mixed-first", "mixed-later"):
            prove_row(
                f"mixed-{label}-exact-proof",
                mixed_by_contract[contracts[label]],
            )
        after_mixed_projection = read_assignments(
            "mixed-batch-projection",
            (
                contracts["mixed-first"],
                contracts["single"],
                contracts["mixed-later"],
            ),
        )
        for label in ("mixed-first", "mixed-later"):
            projection = after_mixed_projection["assignments"][contracts[label]]
            row = mixed_by_contract[contracts[label]]
            if (
                [entry["record_id"] for entry in projection["history"]]
                != [row["data"]["record_id"]]
                or projection["head_assignment_id"] != row["data"]["record_id"]
                or projection["worker_id"] != actor_worker
            ):
                raise RuntimeError("assignment projection omitted partial success")
        if (
            after_mixed_projection["assignments"][contracts["single"]]["history"][0][
                "worker_id"
            ]
            != prior_worker
        ):
            raise RuntimeError("middle assigned target was mutated by batch")

        # A two-member reordered subset with the same explicit base is a true
        # keyed replay, with no extra fact. The singleton member uses actual key.
        reorder_ids = (contracts["mixed-later"], contracts["mixed-first"])
        reorder_before = fingerprint("mixed-reordered-subset-before")
        _completed, reordered = cli(
            "mixed-reordered-subset-replay",
            "task",
            "assign",
            *reorder_ids,
            actor_worker,
            "--reason",
            reason_mixed,
            "--idempotency-key",
            explicit_base,
        )
        _reordered_data, reordered_rows = _assert_batch(
            reordered,
            contract_ids=reorder_ids,
            worker_id=actor_worker,
            reason=reason_mixed,
            base=explicit_base,
            expected_exit_success=True,
        )
        reorder_after = fingerprint("mixed-reordered-subset-after")
        if reorder_before != reorder_after:
            raise RuntimeError(
                "reordered subset replay wrote duplicate assignment facts"
            )
        expected_keys = {
            row["contract_id"]: row["idempotency_key"]
            for row in mixed_rows
            if row["ok"]
        }
        for row in reordered_rows:
            if row["idempotency_key"] != expected_keys[row["contract_id"]]:
                raise RuntimeError("per-target key changed under reorder/subset")

        member = mixed_by_contract[contracts["mixed-first"]]
        single_replay_before = fingerprint("mixed-single-member-replay-before")
        _completed, member_replay = cli(
            "mixed-single-member-actual-key-replay",
            "task",
            "assign",
            contracts["mixed-first"],
            actor_worker,
            "--reason",
            reason_mixed,
            "--idempotency-key",
            member["idempotency_key"],
        )
        single_replay_after = fingerprint("mixed-single-member-replay-after")
        if (
            member_replay.get("data") != member["data"]
            or single_replay_before != single_replay_after
        ):
            raise RuntimeError("single-member actual-key replay changed signed result")

        changed_before = fingerprint("changed-intent-before")
        _completed, changed = cli(
            "same-key-changed-intent-refused",
            "task",
            "assign",
            contracts["mixed-first"],
            actor_worker,
            "--reason",
            "different reason must not replay",
            "--idempotency-key",
            member["idempotency_key"],
            expected_exit=1,
        )
        changed_after = fingerprint("changed-intent-after")
        if (
            _error_code(helper, changed) not in {"rejected", "invalid_input"}
            or helper._contains_any_id(changed)
            or changed_before != changed_after
        ):
            raise RuntimeError("changed operation intent replayed or wrote facts")

        # This generated-base batch models the important continuous outage
        # case: postcommit pending for the first row prevents later target
        # pre-sync from passing until that exact first row is recovered.
        pending_ids = (
            contracts["pending-first"],
            contracts["pending-middle"],
            contracts["pending-last"],
        )
        pending_before = fingerprint("generated-pending-batch-before")
        pending_command = [
            "task",
            "assign",
            *pending_ids,
            actor_worker,
            "--reason",
            reason_pending,
        ]
        _completed, pending_payload, fault_attempts = relay_fault_batch(
            "generated-batch-first-postcommit-fault",
            pending_command,
            expected_exit=1,
        )
        pending_data, pending_rows = _assert_batch(
            pending_payload,
            contract_ids=pending_ids,
            worker_id=actor_worker,
            reason=reason_pending,
            expected_exit_success=False,
        )
        generated_base = pending_data["batch_idempotency_key"]
        if not generated_base.startswith("task-bulk-v1:assign:"):
            raise RuntimeError("generated batch base is not a versioned assignment key")
        if pending_rows[0].get("outcome") != "redis_sync_pending":
            raise RuntimeError("first target did not report accepted relay-pending")
        pending_row = pending_rows[0]
        if any(row.get("outcome") != "not_attempted" for row in pending_rows[1:]):
            raise RuntimeError(
                "later targets were misreported after predecessor relay failure"
            )
        pending_after = fingerprint("generated-pending-first-durable")
        if pending_after["revision"] <= pending_before["revision"]:
            raise RuntimeError(
                "accepted-pending target was not durable before catch-up"
            )
        prove_row("pending-first-exact-before-replay", pending_row)
        pending_projection = read_assignments(
            "pending-projection-before-replay", pending_ids
        )
        if (
            [
                item["record_id"]
                for item in pending_projection["assignments"][pending_ids[0]]["history"]
            ]
            != [pending_row["data"]["record_id"]]
            or pending_projection["assignments"][pending_ids[1]]["history"]
            or pending_projection["assignments"][pending_ids[2]]["history"]
        ):
            raise RuntimeError(
                "pending batch advanced later targets before first replay"
            )
        feed_before = run(
            "pending-redis-feed-before-replay",
            [
                str(python),
                "-c",
                helper._FEED_PROOF,
                genesis_id,
                pending_row["data"]["record_id"],
                "claim.assignment.changed",
                str(pending_before["revision"]),
            ],
        )[1]
        if not isinstance(feed_before, dict) or feed_before.get("matches"):
            raise RuntimeError(
                "accepted-pending event reached native Redis before proof"
            )

        # Recover precisely one original batch member with its returned target
        # key; this also catches up the durable event without a new fact.
        original_key = pending_row["idempotency_key"]
        pending_replay_before = fingerprint("pending-singleton-replay-before")
        _completed, pending_replay = cli(
            "pending-singleton-actual-key-replay",
            "task",
            "assign",
            pending_ids[0],
            actor_worker,
            "--reason",
            reason_pending,
            "--idempotency-key",
            original_key,
        )
        pending_replay_after = fingerprint("pending-singleton-replay-after")
        if (
            pending_replay.get("data") != pending_row["data"]
            or pending_replay.get("revision") != pending_payload["revision"]
            or pending_replay_before != pending_replay_after
        ):
            raise RuntimeError(
                "singleton recovery changed or duplicated original facts"
            )
        prove_row(
            "pending-singleton-replay-exact-proof",
            {
                "contract_id": pending_ids[0],
                "idempotency_key": original_key,
                "candidate_id": pending_row["candidate_id"],
                "fact_ids": pending_row["fact_ids"],
                "data": pending_row["data"],
            },
        )
        feed_after = run(
            "pending-redis-feed-after-singleton-replay",
            [
                str(python),
                "-c",
                helper._FEED_PROOF,
                genesis_id,
                pending_row["data"]["record_id"],
                "claim.assignment.changed",
                str(pending_before["revision"]),
            ],
        )[1]
        if (
            not isinstance(feed_after, dict)
            or len(feed_after.get("matches", [])) != 1
            or feed_after.get("watermark", 0) < pending_row["data"]["revision"]
        ):
            raise RuntimeError("singleton replay failed to synchronize original fact")

        # Resume only untouched siblings with the original generated batch
        # base, then replay the whole original batch in a reordered order.
        later_ids = (pending_ids[2], pending_ids[1])
        later_before = fingerprint("pending-later-subset-before")
        _completed, later_payload = cli(
            "pending-later-siblings-same-base",
            "task",
            "assign",
            *later_ids,
            actor_worker,
            "--reason",
            reason_pending,
            "--idempotency-key",
            generated_base,
        )
        later_data, later_rows = _assert_batch(
            later_payload,
            contract_ids=later_ids,
            worker_id=actor_worker,
            reason=reason_pending,
            base=generated_base,
            expected_exit_success=True,
        )
        later_after = fingerprint("pending-later-subset-after")
        if later_after["revision"] <= later_before["revision"]:
            raise RuntimeError("same-base later subset did not commit fresh targets")
        if any(
            row["idempotency_key"]
            != pending_rows[pending_ids.index(row["contract_id"])]["idempotency_key"]
            for row in later_rows
        ):
            raise RuntimeError("generated target keys changed for later subset")

        whole_ids = (pending_ids[2], pending_ids[0], pending_ids[1])
        whole_before = fingerprint("pending-full-reordered-replay-before")
        _completed, whole_payload = cli(
            "pending-full-batch-reordered-replay",
            "task",
            "assign",
            *whole_ids,
            actor_worker,
            "--reason",
            reason_pending,
            "--idempotency-key",
            generated_base,
        )
        _whole_data, whole_rows = _assert_batch(
            whole_payload,
            contract_ids=whole_ids,
            worker_id=actor_worker,
            reason=reason_pending,
            base=generated_base,
            expected_exit_success=True,
        )
        whole_after = fingerprint("pending-full-reordered-replay-after")
        if whole_before != whole_after:
            raise RuntimeError("full batch replay created duplicate assignment facts")
        if {row["contract_id"]: row["data"]["record_id"] for row in whole_rows} != {
            row["contract_id"]: row["data"]["record_id"]
            for row in (pending_row, *later_rows)
        }:
            raise RuntimeError("reordered full replay changed an original fact ID")

        # The full batch is only considered synchronized after every durable
        # member has its exact event in the native Redis feed.
        for member_row in (pending_row, *later_rows):
            member_feed = run(
                f"final-member-feed-{member_row['contract_id']}",
                [
                    str(python),
                    "-c",
                    helper._FEED_PROOF,
                    genesis_id,
                    member_row["data"]["record_id"],
                    "claim.assignment.changed",
                    str(pending_before["revision"]),
                ],
            )[1]
            if (
                not isinstance(member_feed, dict)
                or len(member_feed.get("matches", [])) != 1
                or member_feed["matches"][0].get("record_id")
                != member_row["data"]["record_id"]
                or member_feed["matches"][0].get("revision")
                != member_row["data"]["revision"]
                or member_feed.get("watermark", 0) < member_row["data"]["revision"]
            ):
                raise RuntimeError(
                    "a batch assignment member is absent from Redis feed"
                )

        for row in (pending_row, *later_rows):
            proof = prove_row("pending-final-exact-projection-proof", row)
            if proof["record_id"] != row["data"]["record_id"]:
                raise RuntimeError("final exact assignment member proof changed")

        # Reopen, rebuild existing materializations, and verify both canonical
        # assignment history and semantic projection remain identical.
        final_contracts = (
            contracts["single"],
            contracts["mixed-first"],
            contracts["mixed-later"],
            *pending_ids,
        )
        projection_before_rebuild = read_assignments(
            "assignment-projection-before-rebuild", final_contracts
        )
        rebuild_before = fingerprint("rebuild-before")
        _completed, rebuild_result = run(
            "runtime-materialization-rebuild",
            [str(python), "-c", _REBUILD, str(database)],
        )
        rebuild_after = fingerprint("rebuild-after")
        projection_after_rebuild = read_assignments(
            "assignment-projection-after-rebuild", final_contracts
        )
        if (
            projection_before_rebuild["assignments"]
            != projection_after_rebuild["assignments"]
            or rebuild_result["before"] != rebuild_result["after"]
            or rebuild_before["tables"]["runtime_records"]
            != rebuild_after["tables"]["runtime_records"]
        ):
            raise RuntimeError("reopen/rebuild changed signed assignment semantics")

        final_feed = run(
            "final-assignment-feed-proof",
            [
                str(python),
                "-c",
                helper._FEED_PROOF,
                genesis_id,
                pending_row["data"]["record_id"],
                "claim.assignment.changed",
                str(pending_before["revision"]),
            ],
        )[1]
        if (
            not isinstance(final_feed, dict)
            or len(final_feed.get("matches", [])) != 1
            or final_feed.get("watermark", 0) < pending_row["data"]["revision"]
        ):
            raise RuntimeError("final Redis feed lacks exact pending assignment event")

        status = "complete"
        ids.update(
            {
                "explicit_batch_base": explicit_base,
                "generated_batch_base": generated_base,
                "mixed_success_fact_ids": [
                    mixed_by_contract[contracts["mixed-first"]]["data"]["record_id"],
                    mixed_by_contract[contracts["mixed-later"]]["data"]["record_id"],
                ],
                "pending_fact_ids": [
                    pending_row["data"]["record_id"],
                    *[row["data"]["record_id"] for row in later_rows],
                ],
                "pending_candidate_ids": [
                    pending_row["candidate_id"],
                    *[row["candidate_id"] for row in later_rows],
                ],
                "fault_attempt_count": len(fault_attempts),
                "sqlite_table_count": len(rebuild_after["tables"]),
                "final_revision": rebuild_after["revision"],
                "worker_invocations": 0,
                "provider_calls": 0,
                "human_output": "not_tested_by_installed_helper",
                "source_only_controls_not_run": [
                    "64-target actual signed batch",
                    "malformed UTF-8/surrogate ID",
                    "plain ValueError/KeyError/TypeError unknown-stop injection",
                    "current Worker key revocation and claim/terminal fence matrix",
                ],
            }
        )
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        manifest = {
            "schema": "aigineering.installed-bulk-assignment.v1",
            "status": status,
            "current_phase": current_phase,
            "helper_sha256": _sha256(Path(__file__).read_bytes()),
            "workspace": str(workspace),
            "database": str(database),
            "domain_name": domain_name,
            "redis_database": redis_db,
            "provider_invoked": False,
            "worker_invoked": False,
            "provider_credentials_in_child_env": False,
            "source_external_binding": (
                "origin checks same-venv package binding; caller must verify "
                "exclusion of the actual source checkout"
            ),
            "install_binding": install_binding,
            "ids": ids,
            "error_type": error_type,
            "commands": commands,
        }
        helper._write_exclusive(
            manifest_path,
            (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        )
    if status != "complete":
        raise RuntimeError("installed bulk assignment control did not complete")
    return {
        "status": status,
        "evidence_dir": str(evidence),
        "domain_name": domain_name,
        "contract_ids": list(contracts.values()),
        "assignment_fact_ids": [
            ids["single_fact_id"],
            *ids["mixed_success_fact_ids"],
            *ids["pending_fact_ids"],
        ],
        "sqlite_table_count": ids["sqlite_table_count"],
        "worker_invocations": 0,
        "provider_calls": 0,
    }
