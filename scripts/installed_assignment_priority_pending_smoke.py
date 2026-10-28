"""Source-external installed controls for assignment/priority relay-pending replay.

This helper uses ordinary packaged CLI operations and a local Redis TCP proxy
that rejects only the exact post-commit assignment/priority event append. It
never invokes a Worker or provider. Raw command streams and database evidence
are private; the returned object contains only safe IDs and phase summaries.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import threading
from urllib.parse import urlsplit, urlunsplit
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
_PROVIDER_KEYS = (
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
)
_FAULT_TYPES = {"claim.assignment.changed", "task.priority.changed"}

_FINGERPRINT = r"""
import hashlib,json,sqlite3,sys
from pathlib import Path
path=Path(sys.argv[1]).absolute()
conn=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
try:
 conn.execute("BEGIN")
 names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
 tables={}
 for name in names:
  q='"'+name.replace('"','""')+'"'
  columns=[r[1] for r in conn.execute('PRAGMA table_info('+q+')')]
  rows=conn.execute('SELECT * FROM '+q).fetchall()
  normalized=[[v.hex() if isinstance(v,bytes) else v for v in row] for row in rows]
  normalized.sort(key=lambda row:json.dumps(row,sort_keys=True,separators=(',',':'),default=str))
  digest=hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
  tables[name]={'columns':columns,'count':len(rows),'sha256':digest}
 revision=int(conn.execute('SELECT COALESCE(MAX(revision),0) FROM runtime_records').fetchone()[0])
 canonical=json.dumps(tables,sort_keys=True,separators=(',',':')).encode()
 print(json.dumps({'revision':revision,'tables':tables,'sha256':hashlib.sha256(canonical).hexdigest()},sort_keys=True))
finally:
 conn.close()
"""

_WORKER_PUBLIC_KEY = r"""
import json
from aigineering.core.signing import Ed25519Signer
signer=Ed25519Signer()
print(json.dumps({'public_key':signer.signer_id,'signature_kind':signer.kind}))
"""

_PROVE_FACT = r"""
import json,sys
from aigineering.core.sqlite_store import SQLiteStore
path,kind,contract_id,record_id,candidate_id,expected_previous,expected_value=sys.argv[1:]
s=SQLiteStore.snapshot_from_path(path)
try:
 pairs=tuple(s.scan_runtime_records())
 exact=[(rev,r) for rev,r in pairs if r.id==record_id]
 if len(exact)!=1: raise RuntimeError('exact direct fact is missing or ambiguous')
 fact_revision,fact=exact[0]
 expected_type={'assignment':'claim.assignment.changed','priority':'task.priority.changed'}[kind]
 expected_effect={'assignment':'task.assign','priority':'task.priority.set'}[kind]
 if fact.record_type!=expected_type: raise RuntimeError('direct fact type differs')
 p=dict(fact.payload)
 if p.get('candidate_id')!=candidate_id or p.get('contract_id')!=contract_id: raise RuntimeError('direct fact identity differs')
 field='previous_assignment_id' if kind=='assignment' else 'previous_priority_id'
 if p.get(field)!=expected_previous: raise RuntimeError('original previous-head value differs')
 value_field='worker_id' if kind=='assignment' else 'priority'
 actual_value=p.get(value_field)
 if kind=='priority':
  if str(actual_value)!=expected_value: raise RuntimeError('priority value differs')
 elif actual_value!=expected_value: raise RuntimeError('worker value differs')
 receipts=[r for _,r in pairs if r.record_type=='candidate.received' and r.payload.get('candidate_id')==candidate_id]
 heads=[r for _,r in pairs if r.record_type=='candidate.committed' and r.payload.get('candidate_id')==candidate_id]
 if len(receipts)!=1 or len(heads)!=1: raise RuntimeError('exact Candidate receipt/head is missing or ambiguous')
 receipt,head=receipts[0],heads[0]
 if list(receipt.payload.get('effect_types',()))!=[expected_effect]: raise RuntimeError('receipt does not bind the expected single effect')
 if receipt.id not in fact.causal_parents: raise RuntimeError('direct fact is not causally bound to receipt')
 if fact.id not in head.payload.get('committed_record_ids',()): raise RuntimeError('direct fact is not a committed-head member')
 if receipt.id not in head.causal_parents or fact.id not in head.causal_parents: raise RuntimeError('committed head parents differ')
 if head.causal_parents[0]!=receipt.id: raise RuntimeError('committed head does not start with exact receipt')
 print(json.dumps({'candidate_id':candidate_id,'record_id':record_id,'record_type':fact.record_type,'contract_id':contract_id,'previous':p[field],'value':actual_value,'fact_revision':int(fact_revision),'store_revision':s.get_runtime_revision(),'receipt_id':receipt.id,'head_id':head.id,'record_count':len(pairs)},sort_keys=True))
finally:
 s.close()
"""

_FEED_PROOF = r"""
import json,os,sys
from aigineering.adapters.redis_query import redis_client
from aigineering.core.ids import compute_content_hash
domain_id,record_id,record_type,after_revision=sys.argv[1:]
url=os.environ['AIGINEERING_REDIS_URL']
root='aig:collaboration:v2:'+compute_content_hash(domain_id)
client=redis_client(url)
meta=root+':meta'
stream=root+':events'
watermark=int(client.hget(meta,'watermark') or '0')
rows=client.xrange(stream,min='-',max='+')
matches=[]
for entry_id,fields in rows:
 revision=int(fields.get('revision','0'))
 if revision>int(after_revision) and fields.get('record_id')==record_id and fields.get('record_type')==record_type:
  matches.append({'stream_id':entry_id,'revision':revision,'record_id':fields['record_id'],'record_type':fields['record_type']})
print(json.dumps({'watermark':watermark,'matches':matches},sort_keys=True))
"""


def _write_exclusive(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_resp_command(stream) -> tuple[list[bytes], bytes]:
    first = stream.readline()
    if not first:
        return [], b""
    if not first.startswith(b"*"):
        raise ValueError("proxy expected a RESP array")
    count = int(first[1:-2])
    raw = bytearray(first)
    args: list[bytes] = []
    for _ in range(count):
        header = stream.readline()
        if not header.startswith(b"$"):
            raise ValueError("proxy expected a bulk argument")
        size = int(header[1:-2])
        content = stream.read(size + 2)
        if len(content) != size + 2 or content[-2:] != b"\r\n":
            raise ConnectionError("proxy received truncated RESP input")
        raw.extend(header)
        raw.extend(content)
        args.append(content[:-2])
    return args, bytes(raw)


def _read_resp_reply(stream) -> bytes:
    first = stream.read(1)
    if not first:
        raise ConnectionError("Redis upstream closed before reply")
    line = stream.readline()
    raw = first + line
    if first in (b"+", b"-", b":", b",", b"(", b"#", b"_"):
        return raw
    if first in (b"$", b"!", b"="):
        length = int(line[:-2])
        if length < 0:
            return raw
        body = stream.read(length + 2)
        if len(body) != length + 2:
            raise ConnectionError("Redis upstream returned a truncated bulk reply")
        return raw + body
    if first in (b"*", b"~", b">", b"%", b"|"):
        count = int(line[:-2])
        if count < 0:
            return raw
        for _ in range(count * (2 if first in (b"%", b"|") else 1)):
            raw += _read_resp_reply(stream)
        return raw
    raise ValueError("proxy received an unsupported RESP reply")


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, upstream, faults):
        self.upstream = upstream
        self.faults = faults
        super().__init__(address, _RelayHandler)


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream_url = self.server.upstream
        try:
            upstream = socket.create_connection(
                (upstream_url.hostname, upstream_url.port or 6379), timeout=5
            )
            upstream.settimeout(15)
        except OSError:
            self.request.sendall(b"-ERR isolated relay upstream unavailable\r\n")
            return
        with (
            upstream,
            self.request.makefile("rb") as incoming,
            upstream.makefile("rb") as outgoing,
        ):
            while True:
                try:
                    args, raw = _read_resp_command(incoming)
                    if not raw:
                        return
                    if (
                        len(args) >= 8
                        and args[0].upper() == b"EVAL"
                        and b"XADD" in args[1]
                        and args[7].decode("utf-8", errors="ignore") in _FAULT_TYPES
                    ):
                        record_type = args[7].decode("utf-8")
                        self.server.faults.append(record_type)
                        self.request.sendall(
                            b"-ERR isolated postcommit relay fault\r\n"
                        )
                        return
                    upstream.sendall(raw)
                    self.request.sendall(_read_resp_reply(outgoing))
                except (ConnectionError, OSError, ValueError):
                    return


def _relay_proxy(redis_url: str):
    upstream = urlsplit(redis_url)
    if (
        upstream.scheme != "redis"
        or upstream.hostname not in {"localhost", "127.0.0.1", "::1"}
        or upstream.query
        or upstream.fragment
    ):
        raise RuntimeError("pending control requires a local plain redis:// URL")
    server = _RelayServer(("127.0.0.1", 0), upstream, [])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    userinfo = upstream.netloc.rsplit("@", 1)[0] + "@" if "@" in upstream.netloc else ""
    proxy_url = urlunsplit(
        (
            "redis",
            f"{userinfo}127.0.0.1:{server.server_address[1]}",
            upstream.path,
            "",
            "",
        )
    )
    return server, thread, proxy_url


def _json_error_code(payload: object) -> str | None:
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return None
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    return None


def _contains_any_id(payload: object) -> bool:
    if isinstance(payload, dict):
        if any(
            key in payload
            for key in ("candidate_id", "record_id", "fact_ids", "idempotency_key")
        ):
            return True
        return any(_contains_any_id(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_any_id(value) for value in payload)
    return False


def _validate_result(
    payload: object,
    *,
    kind: str,
    contract_id: str,
    previous: str,
    expected_value: object,
) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"ok", "data", "revision"}
        or payload.get("ok") is not True
    ):
        raise RuntimeError("success result envelope is not canonical")
    if type(payload.get("revision")) is not int:
        raise RuntimeError("success result revision is not an integer")
    data = payload["data"]
    if not isinstance(data, dict):
        raise RuntimeError("success result data is missing")
    expected_record_type = (
        "claim.assignment.changed" if kind == "assignment" else "task.priority.changed"
    )
    value_key = "worker_id" if kind == "assignment" else "priority"
    if (
        data.get("contract_id") != contract_id
        or data.get("record_type") != expected_record_type
        or data.get(
            "previous_assignment_id" if kind == "assignment" else "previous_priority_id"
        )
        != previous
        or data.get(value_key) != expected_value
        or not isinstance(data.get("record_id"), str)
        or not data.get("record_id")
        or not isinstance(data.get("candidate_id"), str)
        or not data.get("candidate_id")
        or not isinstance(data.get("idempotency_key"), str)
        or not data.get("idempotency_key")
        or data.get("fact_ids") != [data.get("record_id")]
        or type(data.get("revision")) is not int
    ):
        raise RuntimeError("success data does not bind the expected signed result")
    if kind == "assignment" and data.get("assignment_id") != data.get("record_id"):
        raise RuntimeError("assignment ID differs from its direct fact")
    return payload


def _validate_pending(
    payload: object,
    *,
    kind: str,
    contract_id: str,
    previous: str,
    expected_value: object,
) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "ok",
            "error",
            "message",
            "candidate_accepted",
            "facts_retained",
            "data",
            "revision",
        }
        or payload.get("ok") is not False
        or payload.get("error") != "redis_sync_pending"
        or payload.get("candidate_accepted") is not True
        or payload.get("facts_retained") is not True
        or not isinstance(payload.get("message"), str)
        or not isinstance(payload.get("data"), dict)
        or type(payload.get("revision")) is not int
    ):
        raise RuntimeError("postcommit pending envelope lost accepted result")
    data = payload["data"]
    _validate_result(
        {"ok": True, "data": data, "revision": payload["revision"]},
        kind=kind,
        contract_id=contract_id,
        previous=previous,
        expected_value=expected_value,
    )
    return payload


def installed_assignment_priority_pending_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Run source-external signed CLI controls for accepted relay-pending replay.

    This function is intended for the root's installed-artifact harness. It
    performs no provider/Worker invocation, uses a new workspace/domain, and
    only injects relay failures through a loopback proxy targeting the exact
    assignment/priority append event. It returns summaries, never raw streams.
    """
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    parsed = urlsplit(redis_url)
    if (
        not redis_url
        or parsed.scheme != "redis"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.query
        or parsed.fragment
        or (parsed.path and not parsed.path.lstrip("/").isdigit())
    ):
        raise RuntimeError("explicit local Redis URL is required")

    root = cwd / f"installed-assignment-priority-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    workspace = root / "workspace"
    caller = root / "caller"
    evidence = root / "private-evidence"
    for directory in (workspace, caller, evidence):
        directory.mkdir(mode=0o700)
    identity = workspace / ".aig" / "identity"
    database = workspace / ".aig" / "store.db"
    commands: list[dict] = []
    status = "failed"
    current_phase: str | None = None
    ids: dict[str, object] = {}
    error_type: str | None = None
    install_binding: dict[str, str] = {}
    source_only_controls_not_run = [
        "signed Candidate rejection with relay pending",
        "unknown internal exception safe classification and chained cause",
        "current operator key revocation replay denial",
        "claim/terminal fence matrix",
        "human-format pending result",
    ]
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env.pop("PYTHONHOME", None)
    local_env["PYTHONNOUSERSITE"] = "1"
    for variable in tuple(local_env):
        if variable in _PROVIDER_KEYS or variable.endswith("_API_KEY"):
            local_env.pop(variable, None)
    for variable in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_OPENAI_AD_TOKEN",
    ):
        local_env.pop(variable, None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")
    worker_id = f"worker:assignment-priority-{uuid.uuid4().hex[:12]}"
    worker_id_two = f"worker:assignment-priority-two-{uuid.uuid4().hex[:12]}"
    worker_key_id = f"worker-key-{uuid.uuid4().hex[:12]}"
    worker_key_id_two = f"worker-key-{uuid.uuid4().hex[:12]}"
    domain_name = f"assignment-priority-{uuid.uuid4().hex}"

    def run(
        phase: str,
        argv: list[str],
        *,
        expected_exit: int = 0,
        parse_json: bool = True,
        command_env: dict[str, str] | None = None,
    ):
        nonlocal current_phase
        current_phase = phase
        ordinal = len(commands) + 1
        try:
            proc = subprocess.Popen(
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
                    "stdout_sha256": _digest(stdout),
                    "stderr_sha256": _digest(stderr),
                    "expected_exit": expected_exit,
                }
            )
            raise RuntimeError(
                "phase process could not be started; streams retained"
            ) from None
        try:
            stdout, stderr = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            tail_out, tail_err = proc.communicate(timeout=10)
            stdout = tail_out or stdout
            stderr = tail_err or stderr
            out_path = evidence / f"{ordinal:03d}-{phase}.stdout.raw"
            err_path = evidence / f"{ordinal:03d}-{phase}.stderr.raw"
            _write_exclusive(out_path, stdout)
            _write_exclusive(err_path, stderr)
            commands.append(
                {
                    "phase": phase,
                    "argv": argv,
                    "exit": None,
                    "timed_out": True,
                    "stdout_path": out_path.name,
                    "stderr_path": err_path.name,
                    "stdout_sha256": _digest(stdout),
                    "stderr_sha256": _digest(stderr),
                }
            )
            raise RuntimeError("subprocess timed out; streams retained") from None
        out_path = evidence / f"{ordinal:03d}-{phase}.stdout.raw"
        err_path = evidence / f"{ordinal:03d}-{phase}.stderr.raw"
        _write_exclusive(out_path, stdout)
        _write_exclusive(err_path, stderr)
        commands.append(
            {
                "phase": phase,
                "argv": argv,
                "exit": proc.returncode,
                "timed_out": False,
                "stdout_path": out_path.name,
                "stderr_path": err_path.name,
                "stdout_sha256": _digest(stdout),
                "stderr_sha256": _digest(stderr),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
                "expected_exit": expected_exit,
            }
        )
        if proc.returncode != expected_exit:
            raise RuntimeError(f"phase {phase} returned an unexpected exit status")
        completed = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        if not parse_json:
            return completed, None
        try:
            return completed, json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(f"phase {phase} did not return JSON") from None

    def cli(phase: str, *args: str, expected_exit: int = 0, command_env=None):
        return run(
            phase,
            [str(aig), "-C", str(workspace), *args, "--json"],
            expected_exit=expected_exit,
            command_env=command_env,
        )

    def fingerprint(phase: str) -> dict:
        _completed, payload = run(
            phase,
            [str(python), "-c", _FINGERPRINT, str(database)],
        )
        if not isinstance(payload, dict) or set(payload.get("tables", {})) != _TABLES:
            raise RuntimeError(f"{phase} did not fingerprint all 18 Store tables")
        return payload

    def prove(
        phase: str,
        result: dict,
        *,
        kind: str,
        contract_id: str,
        previous: str,
        expected_value: object,
    ) -> dict:
        before = fingerprint(f"{phase}-before")
        data = result.get("data")
        _completed, proof = run(
            phase,
            [
                str(python),
                "-c",
                _PROVE_FACT,
                str(database),
                kind,
                contract_id,
                data["record_id"],
                data["candidate_id"],
                previous,
                str(expected_value),
            ],
        )
        after = fingerprint(f"{phase}-after")
        if before != after:
            raise RuntimeError(f"{phase} read-only proof changed SQLite")
        if (
            not isinstance(proof, dict)
            or proof.get("record_id") != data["record_id"]
            or proof.get("candidate_id") != data["candidate_id"]
            or proof.get("fact_revision") != data["revision"]
            or proof.get("store_revision") != result["revision"]
            or proof.get("previous") != previous
            or proof.get("value") != expected_value
            or proof.get("record_type") != data["record_type"]
            or proof.get("contract_id") != contract_id
        ):
            raise RuntimeError(f"{phase} exact durable witness does not match CLI data")
        return proof

    def relay_pending(
        phase: str,
        command: list[str],
        *,
        expected_type: str,
        expected_error_exit: int = 1,
    ):
        if expected_type not in _FAULT_TYPES:
            raise RuntimeError("unsupported relay fault type")
        server, thread, proxy_url = _relay_proxy(redis_url)
        command_env = dict(local_env)
        command_env["AIGINEERING_REDIS_URL"] = proxy_url
        try:
            _completed, payload = run(
                phase,
                [str(aig), "-C", str(workspace), *command, "--json"],
                expected_exit=expected_error_exit,
                command_env=command_env,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        if server.faults != [expected_type]:
            raise RuntimeError(f"{phase} did not isolate exactly the target event")
        return payload

    def assert_preflight_no_candidate(contract_id: str) -> None:
        before = fingerprint("preflight-redis-before")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            unavailable_port = probe.getsockname()[1]
        unavailable_env = dict(local_env)
        unavailable_env["AIGINEERING_REDIS_URL"] = (
            f"redis://127.0.0.1:{unavailable_port}/{parsed.path.lstrip('/') or '0'}"
        )
        _completed, payload = cli(
            "preflight-redis-unavailable",
            "task",
            "assign",
            contract_id,
            worker_id,
            "--reason",
            "preflight control",
            expected_exit=1,
            command_env=unavailable_env,
        )
        after = fingerprint("preflight-redis-after")
        if before != after:
            raise RuntimeError("Redis preflight failure changed SQLite")
        if (
            _json_error_code(payload) != "redis_unavailable"
            or _contains_any_id(payload)
            or not isinstance(payload, dict)
            or (
                "candidate_accepted" in payload
                and payload.get("candidate_accepted") is not None
            )
            or (
                "facts_retained" in payload
                and payload.get("facts_retained") is not False
            )
            or "data" in payload
            or "revision" in payload
        ):
            raise RuntimeError("preflight failure fabricated accepted operation data")

    manifest_path = evidence / "manifest.json"
    try:
        # Bind both executable and package imports to the same source-external install.
        _completed, origin = run(
            "installed-origin",
            [
                str(python),
                "-c",
                "import json,pathlib,sys,aigineering;print(json.dumps({'prefix':str(pathlib.Path(sys.prefix).absolute()),'module':str(pathlib.Path(aigineering.__file__).absolute())}))",
            ],
        )
        prefix = Path(origin["prefix"]).resolve()
        module = Path(origin["module"]).resolve()
        if (
            aig.resolve().parent != prefix / "bin"
            or not module.is_relative_to(prefix)
            or "site-packages" not in str(module)
        ):
            raise RuntimeError(
                "CLI/Python are not bound to one source-external install"
            )
        install_binding = {
            "python_prefix": str(prefix),
            "module_path": str(module),
            "aig_path": str(aig.resolve()),
        }
        identity.mkdir(parents=True, mode=0o700, exist_ok=True)

        _completed, domain_payload = cli(
            "domain-init", "domain", "init", "--domain", domain_name
        )
        if not database.is_file():
            raise RuntimeError("domain init did not create its isolated Store")
        domain_id = (
            domain_payload.get("domain_id")
            if isinstance(domain_payload, dict)
            else None
        )
        if not isinstance(domain_id, str) or not domain_id:
            raise RuntimeError("domain init did not return the immutable Genesis ID")
        _completed, worker_key = run(
            "worker-public-key",
            [
                str(python),
                "-c",
                _WORKER_PUBLIC_KEY,
            ],
        )
        if not isinstance(worker_key, dict) or not worker_key.get("public_key"):
            raise RuntimeError("local Worker public key setup failed")
        cli(
            "worker-register",
            "worker",
            "register",
            "--worker-id",
            worker_id,
            "--capacity",
            "2",
            "--key-id",
            worker_key_id,
            "--public-key",
            worker_key["public_key"],
        )
        _completed, worker_key_two = run(
            "worker-public-key-two",
            [
                str(python),
                "-c",
                _WORKER_PUBLIC_KEY,
            ],
        )
        if not isinstance(worker_key_two, dict) or not worker_key_two.get("public_key"):
            raise RuntimeError("second local Worker public key setup failed")
        cli(
            "worker-register-two",
            "worker",
            "register",
            "--worker-id",
            worker_id_two,
            "--capacity",
            "2",
            "--key-id",
            worker_key_id_two,
            "--public-key",
            worker_key_two["public_key"],
        )

        task_a_completed, task_a = cli(
            "task-create-assignment-success",
            "task",
            "create",
            "--name",
            f"assignment-priority-a-{uuid.uuid4().hex[:8]}",
            "--description",
            "Installed assignment replay control.",
            "--budget",
            "3",
        )
        task_b_completed, task_b = cli(
            "task-create-assignment-pending",
            "task",
            "create",
            "--name",
            f"assignment-priority-b-{uuid.uuid4().hex[:8]}",
            "--description",
            "Installed assignment and priority pending control.",
            "--budget",
            "3",
        )
        contract_a = task_a.get("contract_id") if isinstance(task_a, dict) else None
        contract_b = task_b.get("contract_id") if isinstance(task_b, dict) else None
        if (
            not isinstance(contract_a, str)
            or not isinstance(contract_b, str)
            or contract_a == contract_b
        ):
            raise RuntimeError(
                "ordinary task creation did not return two distinct Contract IDs"
            )
        ids["domain_name"] = domain_name
        ids["contract_a"] = contract_a
        ids["contract_b"] = contract_b
        ids["worker_id"] = worker_id
        ids["worker_id_two"] = worker_id_two
        ids["genesis_id"] = domain_id

        assert_preflight_no_candidate(contract_b)

        # First default-generated assignment succeeds. Replay with the returned
        # actual key and prove no durable rows changed.
        assign_a_args = [
            "task",
            "assign",
            contract_a,
            worker_id,
            "--reason",
            "default assignment replay",
        ]
        assign_a_before = fingerprint("assignment-default-before")
        _completed, assign_a = cli("assignment-default-success", *assign_a_args)
        assign_a = _validate_result(
            assign_a,
            kind="assignment",
            contract_id=contract_a,
            previous="",
            expected_value=worker_id,
        )
        if not assign_a["data"]["idempotency_key"].startswith("task-assignment:"):
            raise RuntimeError(
                "default assignment key did not preserve generated prefix"
            )
        assign_a_after = fingerprint("assignment-default-after")
        if (
            assign_a_after["revision"] != assign_a["revision"]
            or assign_a_after["revision"] <= assign_a_before["revision"]
        ):
            raise RuntimeError(
                "successful assignment revision is not its durable Store revision"
            )
        prove(
            "assignment-default-durable-proof",
            assign_a,
            kind="assignment",
            contract_id=contract_a,
            previous="",
            expected_value=worker_id,
        )
        replay_args = [
            *assign_a_args,
            "--idempotency-key",
            assign_a["data"]["idempotency_key"],
        ]
        before_keyed_replay = fingerprint("assignment-default-key-replay-before")
        _completed, assign_a_replay = cli("assignment-default-key-replay", *replay_args)
        after_keyed_replay = fingerprint("assignment-default-key-replay-after")
        if assign_a_replay != assign_a or before_keyed_replay != after_keyed_replay:
            raise RuntimeError("assignment replay changed its original result or Store")

        # Second ordinary initial assignment supplies the immutable previous
        # head for the reassign relay-pending operation.
        initial_key = f"installed-assignment-initial:{uuid.uuid4().hex}"
        _completed, assign_b = cli(
            "assignment-initial-explicit-key",
            "task",
            "assign",
            contract_b,
            worker_id,
            "--reason",
            "initial assignment for reassign",
            "--idempotency-key",
            initial_key,
        )
        assign_b = _validate_result(
            assign_b,
            kind="assignment",
            contract_id=contract_b,
            previous="",
            expected_value=worker_id,
        )
        if assign_b["data"]["idempotency_key"] != initial_key:
            raise RuntimeError("explicit assignment key was not returned unchanged")
        prove(
            "assignment-initial-durable-proof",
            assign_b,
            kind="assignment",
            contract_id=contract_b,
            previous="",
            expected_value=worker_id,
        )
        assert_preflight_no_candidate(contract_b)

        # The post-commit fault targets only the assignment fact append; all
        # preflight and unrelated Redis commands pass through to native Redis.
        reassign_reason = "priority-pending assignment update"
        reassign_command = [
            "task",
            "reassign",
            contract_b,
            "--worker",
            worker_id_two,
            "--previous",
            assign_b["data"]["record_id"],
            "--reason",
            reassign_reason,
        ]
        reassign_before = fingerprint("reassign-pending-before")
        reassign_pending = relay_pending(
            "reassign-postcommit-relay-pending",
            reassign_command,
            expected_type="claim.assignment.changed",
        )
        reassign_pending = _validate_pending(
            reassign_pending,
            kind="assignment",
            contract_id=contract_b,
            previous=assign_b["data"]["record_id"],
            expected_value=worker_id_two,
        )
        if not reassign_pending["data"]["idempotency_key"].startswith(
            "task-assignment:"
        ):
            raise RuntimeError("pending reassign omitted its actual generated key")
        reassign_after = fingerprint("reassign-pending-store")
        if (
            reassign_after["revision"] != reassign_pending["revision"]
            or reassign_after == reassign_before
        ):
            raise RuntimeError(
                "accepted reassign was not durably reflected before catch-up"
            )
        prove(
            "reassign-pending-exact-durable-proof",
            reassign_pending,
            kind="assignment",
            contract_id=contract_b,
            previous=assign_b["data"]["record_id"],
            expected_value=worker_id_two,
        )
        feed_before = run(
            "reassign-feed-before-catchup",
            [
                str(python),
                "-c",
                _FEED_PROOF,
                domain_id,
                reassign_pending["data"]["record_id"],
                "claim.assignment.changed",
                str(reassign_before["revision"]),
            ],
        )[1]
        if (
            not isinstance(feed_before, dict)
            or feed_before.get("matches")
            or type(feed_before.get("watermark")) is not int
            or feed_before["watermark"] >= reassign_pending["revision"]
        ):
            raise RuntimeError(
                "pending assignment fact unexpectedly reached Redis before replay"
            )

        reassign_key = reassign_pending["data"]["idempotency_key"]
        replay_reassign = [*reassign_command, "--idempotency-key", reassign_key]
        before_reassign_replay = fingerprint("reassign-replay-before")
        _completed, reassign_replay = cli(
            "reassign-exact-key-catchup", *replay_reassign
        )
        after_reassign_replay = fingerprint("reassign-replay-after")
        if (
            reassign_replay.get("data") != reassign_pending["data"]
            or reassign_replay.get("revision") != reassign_pending["revision"]
            or before_reassign_replay != after_reassign_replay
        ):
            raise RuntimeError(
                "reassign exact-key replay changed or duplicated durable history"
            )
        prove(
            "reassign-replay-durable-proof",
            reassign_replay,
            kind="assignment",
            contract_id=contract_b,
            previous=assign_b["data"]["record_id"],
            expected_value=worker_id_two,
        )
        feed_after = run(
            "reassign-feed-after-catchup",
            [
                str(python),
                "-c",
                _FEED_PROOF,
                domain_id,
                reassign_pending["data"]["record_id"],
                "claim.assignment.changed",
                str(reassign_before["revision"]),
            ],
        )[1]
        if (
            not isinstance(feed_after, dict)
            or feed_after.get("watermark", 0) < reassign_pending["revision"]
            or len(feed_after.get("matches", [])) != 1
        ):
            raise RuntimeError(
                "exact assignment replay did not catch up its Redis event"
            )

        # Initial explicit priority head, then an update whose exact original
        # previous field is retained by a pending response and replay.
        initial_priority_key = f"installed-priority-initial:{uuid.uuid4().hex}"
        _completed, priority_initial = cli(
            "priority-initial-explicit-key",
            "task",
            "priority",
            contract_b,
            "2",
            "--reason",
            "initial priority",
            "--idempotency-key",
            initial_priority_key,
        )
        priority_initial = _validate_result(
            priority_initial,
            kind="priority",
            contract_id=contract_b,
            previous="",
            expected_value=2,
        )
        if priority_initial["data"]["idempotency_key"] != initial_priority_key:
            raise RuntimeError("explicit priority key was not returned unchanged")
        prove(
            "priority-initial-durable-proof",
            priority_initial,
            kind="priority",
            contract_id=contract_b,
            previous="",
            expected_value=2,
        )

        priority_reason = "pending priority update"
        priority_command = [
            "task",
            "priority",
            contract_b,
            "5",
            "--previous",
            priority_initial["data"]["record_id"],
            "--reason",
            priority_reason,
        ]
        priority_before = fingerprint("priority-pending-before")
        priority_pending = relay_pending(
            "priority-postcommit-relay-pending",
            priority_command,
            expected_type="task.priority.changed",
        )
        priority_pending = _validate_pending(
            priority_pending,
            kind="priority",
            contract_id=contract_b,
            previous=priority_initial["data"]["record_id"],
            expected_value=5,
        )
        if not priority_pending["data"]["idempotency_key"].startswith("task-priority:"):
            raise RuntimeError("pending priority omitted its actual generated key")
        priority_after = fingerprint("priority-pending-store")
        if (
            priority_after["revision"] != priority_pending["revision"]
            or priority_after == priority_before
        ):
            raise RuntimeError("accepted priority was not durable before catch-up")
        prove(
            "priority-pending-exact-durable-proof",
            priority_pending,
            kind="priority",
            contract_id=contract_b,
            previous=priority_initial["data"]["record_id"],
            expected_value=5,
        )
        priority_feed_before = run(
            "priority-feed-before-catchup",
            [
                str(python),
                "-c",
                _FEED_PROOF,
                domain_id,
                priority_pending["data"]["record_id"],
                "task.priority.changed",
                str(priority_before["revision"]),
            ],
        )[1]
        if (
            not isinstance(priority_feed_before, dict)
            or priority_feed_before.get("matches")
            or type(priority_feed_before.get("watermark")) is not int
            or priority_feed_before["watermark"] >= priority_pending["revision"]
        ):
            raise RuntimeError(
                "pending priority fact unexpectedly reached Redis before replay"
            )
        priority_replay = [
            *priority_command,
            "--idempotency-key",
            priority_pending["data"]["idempotency_key"],
        ]
        before_priority_replay = fingerprint("priority-replay-before")
        _completed, priority_recovered = cli(
            "priority-exact-key-catchup", *priority_replay
        )
        after_priority_replay = fingerprint("priority-replay-after")
        if (
            priority_recovered.get("data") != priority_pending["data"]
            or priority_recovered.get("revision") != priority_pending["revision"]
            or before_priority_replay != after_priority_replay
        ):
            raise RuntimeError(
                "priority exact-key replay changed or duplicated durable history"
            )
        prove(
            "priority-replay-durable-proof",
            priority_recovered,
            kind="priority",
            contract_id=contract_b,
            previous=priority_initial["data"]["record_id"],
            expected_value=5,
        )
        priority_feed_after = run(
            "priority-feed-after-catchup",
            [
                str(python),
                "-c",
                _FEED_PROOF,
                domain_id,
                priority_pending["data"]["record_id"],
                "task.priority.changed",
                str(priority_before["revision"]),
            ],
        )[1]
        if (
            not isinstance(priority_feed_after, dict)
            or priority_feed_after.get("watermark", 0) < priority_pending["revision"]
            or len(priority_feed_after.get("matches", [])) != 1
        ):
            raise RuntimeError("exact priority replay did not catch up its Redis event")

        # Changed payload with the old head is a stale-intent negative, not
        # catch-up. It should be refused before adding a new operation fact.
        before_changed_assignment = fingerprint("changed-assignment-intent-before")
        changed_assignment_command = [
            "task",
            "reassign",
            contract_b,
            "--worker",
            worker_id,
            "--previous",
            assign_b["data"]["record_id"],
            "--reason",
            "different intent after pending replay",
            "--idempotency-key",
            reassign_key,
        ]
        _completed, changed_assignment = cli(
            "changed-assignment-intent-rejected",
            *changed_assignment_command,
            expected_exit=1,
        )
        after_changed_assignment = fingerprint("changed-assignment-intent-after")
        if (
            _json_error_code(changed_assignment) not in {"invalid_input", "rejected"}
            or _contains_any_id(changed_assignment)
            or before_changed_assignment != after_changed_assignment
        ):
            raise RuntimeError(
                "changed assignment intent was treated as replay or wrote facts"
            )

        before_changed_priority = fingerprint("changed-priority-intent-before")
        changed_priority_command = [
            "task",
            "priority",
            contract_b,
            "8",
            "--previous",
            priority_initial["data"]["record_id"],
            "--reason",
            "different intent after pending replay",
            "--idempotency-key",
            priority_pending["data"]["idempotency_key"],
        ]
        _completed, changed_priority = cli(
            "changed-priority-intent-rejected",
            *changed_priority_command,
            expected_exit=1,
        )
        after_changed_priority = fingerprint("changed-priority-intent-after")
        if (
            _json_error_code(changed_priority) not in {"invalid_input", "rejected"}
            or _contains_any_id(changed_priority)
            or before_changed_priority != after_changed_priority
        ):
            raise RuntimeError(
                "changed priority intent was treated as replay or wrote facts"
            )

        status = "complete"
        ids.update(
            {
                "assignment_default_fact_id": assign_a["data"]["record_id"],
                "assignment_default_candidate_id": assign_a["data"]["candidate_id"],
                "reassign_fact_id": reassign_pending["data"]["record_id"],
                "reassign_candidate_id": reassign_pending["data"]["candidate_id"],
                "priority_initial_fact_id": priority_initial["data"]["record_id"],
                "priority_pending_fact_id": priority_pending["data"]["record_id"],
                "priority_pending_candidate_id": priority_pending["data"][
                    "candidate_id"
                ],
                "sqlite_table_count": len(priority_after["tables"]),
                "final_revision": after_priority_replay["revision"],
                "worker_invocations": 0,
                "provider_calls": 0,
                "source_oracle_matrix": "not_run_by_installed_helper",
                "source_only_controls_not_run": source_only_controls_not_run,
                "human_pending_output": "not_exercised_by_installed_helper",
                "genesis_id": domain_id,
                "worker_id_two": worker_id_two,
            }
        )
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        manifest = {
            "schema": "aigineering.installed-assignment-priority-pending.v1",
            "status": status,
            "current_phase": current_phase,
            "helper_sha256": _digest(Path(__file__).read_bytes()),
            "workspace": str(workspace),
            "database": str(database),
            "domain_name": domain_name,
            "redis_database": parsed.path.lstrip("/") or "0",
            "provider_invoked": False,
            "worker_invoked": False,
            "provider_credentials_in_child_env": False,
            "source_external_binding": "checked by installed-origin phase",
            "install_binding": install_binding,
            "source_only_controls_not_run": source_only_controls_not_run,
            "human_pending_output": "not_exercised_by_installed_helper",
            "ids": ids,
            "error_type": error_type,
            "commands": commands,
        }
        _write_exclusive(
            manifest_path,
            (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        )
    if status != "complete":
        raise RuntimeError("installed assignment/priority control did not complete")
    return {
        "status": status,
        "evidence_dir": str(evidence),
        "domain_name": domain_name,
        "contract_ids": [ids["contract_a"], ids["contract_b"]],
        "assignment_fact_ids": [
            ids["assignment_default_fact_id"],
            ids["reassign_fact_id"],
        ],
        "priority_fact_ids": [
            ids["priority_initial_fact_id"],
            ids["priority_pending_fact_id"],
        ],
        "sqlite_table_count": ids["sqlite_table_count"],
        "worker_invocations": 0,
        "provider_calls": 0,
    }
