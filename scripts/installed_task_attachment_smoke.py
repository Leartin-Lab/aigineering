"""Source-external installed CLI controls for signed task attachments.

Fixture-backed local runtime evidence only; this helper never invokes a Worker
or provider.  It is intentionally limited to ordinary signed publication,
association, safe listing, and exact-byte export.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import subprocess
import threading
from urllib.parse import urlsplit, urlunsplit
import uuid


_MARKERS = (
    "AIG_PRIVATE_ATTACHMENT_BODY_20261007",
    "AIG_PRIVATE_ATTACHMENT_URI_20261007",
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

_REVOKE_KEY = r"""
import json,sys
from aigineering.local_publication import commit_local_effect,require_accepted
from aigineering.protocol.effect_builders import actor_revocation_effect
from aigineering.task_attachments import open_task_attachment_store
s=open_task_attachment_store(sys.argv[3],writable=True)
try:
 d=require_accepted(commit_local_effect(s,actor_revocation_effect(sys.argv[1],sys.argv[2],'installed attachment replay fence'),idempotency_key='installed-attachment-revoke-v1'))
 print(json.dumps({'candidate_id':d.candidate_id,'accepted':d.accepted}))
finally:s.close()
"""

_PUBLISH_VARIANT = r"""
import json,sys
from dataclasses import replace
from aigineering.business import artifacts
from aigineering.local_publication import commit_local_effect,require_accepted
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.task_attachments import open_task_attachment_store
mode,name,path=sys.argv[1:]
s=open_task_attachment_store(path,writable=True)
try:
 if mode=='sealed':
  asset=replace(artifacts.attachment(name,b'fixture bytes',media_type='application/octet-stream'),disclosure_view='sealed')
 elif mode=='bad_hash':
  base=artifacts.attachment(name,b'fixture bytes',media_type='application/octet-stream')
  value=artifacts.envelope(base);payload=dict(value['payload']);payload['sha256']='0'*64
  asset=artifacts.proposal(name,'attachment',payload)
 elif mode=='wrong_kind':
  asset=artifacts.proposal(name,'report',{'markdown':'fixture','citations':{}})
 else: raise ValueError('unknown fixture mode')
 d=require_accepted(commit_local_effect(s,asset_proposal_effect(asset),idempotency_key='installed-attachment-variant:'+name))
 print(json.dumps({'asset_id':d.assets[0].id,'candidate_id':d.candidate_id,'accepted':d.accepted}))
finally:s.close()
"""

_FINGERPRINT = r"""
import hashlib,json,sqlite3,sys
from pathlib import Path
p=sys.argv[1]
c=sqlite3.connect(Path(p).resolve().as_uri()+"?mode=ro",uri=True)
try:
 c.execute("BEGIN")
 names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
 tables={}
 for n in names:
  q='"'+n.replace('"','""')+'"'
  columns=[r[1] for r in c.execute('PRAGMA table_info('+q+')')]
  rows=c.execute('SELECT * FROM '+q).fetchall()
  norm=[[v.hex() if isinstance(v,bytes) else v for v in row] for row in rows]
  norm.sort(key=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'),default=str))
  b=json.dumps(norm,sort_keys=True,separators=(',',':'),default=str).encode()
  tables[n]={'columns':columns,'count':len(rows),'sha256':hashlib.sha256(b).hexdigest()}
 revision=c.execute('SELECT COALESCE(MAX(revision),0) FROM runtime_records').fetchone()[0]
 print(json.dumps({'revision':int(revision),'tables':tables,'sha256':hashlib.sha256(json.dumps(tables,sort_keys=True,separators=(',',':')).encode()).hexdigest()}))
finally:c.close()
"""

_PROVE_DURABLE_LINK = r"""
import json,sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.task_attachments import validate_link_history
path,contract_id,record_id,asset_id,candidate_id,asset_candidate_id,key=sys.argv[1:]
s=SQLiteStore.snapshot_from_path(path)
try:
 rows=tuple(record for _,record in s.scan_runtime_records())
 matches=tuple(record for record in rows if record.id==record_id)
 if len(matches)!=1: raise SystemExit('expected one exact durable record')
 record=matches[0]
 if record.record_type!='task.attachment.linked': raise SystemExit('wrong durable record type')
 if dict(record.payload)!={'actor_id':record.payload.get('actor_id'),'asset_candidate_id':asset_candidate_id,'asset_head_id':record.payload.get('asset_head_id'),'asset_id':asset_id,'asset_receipt_id':record.payload.get('asset_receipt_id'),'asset_record_id':record.payload.get('asset_record_id'),'candidate_id':candidate_id,'contract_id':contract_id,'contract_record_id':record.payload.get('contract_record_id'),'key_id':record.payload.get('key_id'),'idempotency_key':key}: raise SystemExit('durable payload does not match accepted result')
 validated=validate_link_history(rows,contract_id)
 if sum(item.id==record_id for item in validated)!=1: raise SystemExit('durable link lacks exact validated history')
 print(json.dumps({'record_id':record_id,'record_type':record.record_type,'contract_id':contract_id,'asset_id':asset_id,'candidate_id':candidate_id,'asset_candidate_id':asset_candidate_id,'idempotency_key':key,'revision':s.get_runtime_revision(),'record_count':len(rows)}))
finally:s.close()
"""


def _write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_flat_task_status(payload: object, contract_id: str) -> None:
    required = {
        "ok",
        "contract_id",
        "name",
        "status",
        "terminal",
        "outputs_satisfied",
        "outputs",
        "blockers",
        "dependency_blockers",
        "dependencies",
        "budget_remaining",
        "qualification_pending_outputs",
        "silent_failure_risks",
        "assignment",
        "priority",
        "priority_fact_id",
        "projection_hash",
        "integrity",
    }
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or "revision" in payload
        or "data" in payload
        or payload.get("ok") != (payload.get("status") == "completed")
        or payload.get("contract_id") != contract_id
        or not isinstance(payload.get("name"), str)
        or not isinstance(payload.get("status"), str)
        or type(payload.get("terminal")) is not bool
        or type(payload.get("ok")) is not bool
        or not isinstance(payload.get("outputs"), dict)
        or type(payload.get("outputs_satisfied")) is not bool
        or not isinstance(payload.get("blockers"), list)
        or not isinstance(payload.get("dependency_blockers"), list)
        or not isinstance(payload.get("dependencies"), list)
        or type(payload.get("budget_remaining")) is not int
        or not isinstance(payload.get("qualification_pending_outputs"), dict)
        or not isinstance(payload.get("silent_failure_risks"), list)
        or (
            payload.get("assignment") is not None
            and not isinstance(payload.get("assignment"), dict)
        )
        or (
            payload.get("priority_fact_id") is not None
            and not isinstance(payload.get("priority_fact_id"), str)
        )
        or not isinstance(payload.get("projection_hash"), str)
        or not payload.get("projection_hash")
        or type(payload.get("priority")) is not int
        or not isinstance(payload.get("integrity"), dict)
        or not isinstance(payload["integrity"].get("status"), str)
        or type(payload["integrity"].get("consistent")) is not bool
        or type(payload["integrity"].get("invalid_runtime_record_count")) is not int
        or not isinstance(payload["integrity"].get("checked"), list)
        or not isinstance(payload["integrity"].get("not_checked"), list)
        or (
            payload["integrity"].get("materializations_match") is not None
            and type(payload["integrity"].get("materializations_match")) is not bool
        )
        or (
            payload["integrity"].get("error_type") is not None
            and not isinstance(payload["integrity"].get("error_type"), str)
        )
    ):
        raise RuntimeError("flat task-status response shape is invalid")


_TASK_STATE_FIELDS = (
    "contract_id",
    "name",
    "status",
    "terminal",
    "ok",
    "outputs_satisfied",
    "outputs",
    "blockers",
    "dependency_blockers",
    "dependencies",
    "budget_remaining",
    "qualification_pending_outputs",
    "silent_failure_risks",
    "assignment",
    "priority",
    "priority_fact_id",
    "projection_hash",
    "integrity",
)


def _task_state_projection(payload: object, contract_id: str) -> dict:
    _validate_flat_task_status(payload, contract_id)
    projected = {field: payload[field] for field in _TASK_STATE_FIELDS}
    integrity = payload["integrity"]
    projected["integrity"] = {
        field: integrity[field]
        for field in (
            "status",
            "consistent",
            "invalid_runtime_record_count",
            "materializations_match",
            "error_type",
            "checked",
            "not_checked",
        )
    }
    return projected


def _assert_same_task_state(before: object, after: object, contract_id: str) -> None:
    if _task_state_projection(before, contract_id) != _task_state_projection(
        after, contract_id
    ):
        raise RuntimeError(
            "attachment association changed task execution/readiness state"
        )


def _read_resp_command(stream) -> tuple[list[bytes], bytes]:
    first = stream.readline()
    if not first:
        return [], b""
    if not first.startswith(b"*"):
        raise ValueError("Redis proxy expected an array command")
    count = int(first[1:-2])
    raw = bytearray(first)
    args = []
    for _ in range(count):
        header = stream.readline()
        if not header.startswith(b"$"):
            raise ValueError("Redis proxy expected bulk-string command arguments")
        size = int(header[1:-2])
        data = stream.read(size + 2)
        if len(data) != size + 2 or data[-2:] != b"\r\n":
            raise ValueError("Redis proxy received truncated command")
        raw.extend(header)
        raw.extend(data)
        args.append(data[:-2])
    return args, bytes(raw)


def _read_resp_reply(stream) -> bytes:
    first = stream.read(1)
    if not first:
        raise ConnectionError("Redis upstream closed before response")
    line = stream.readline()
    raw = first + line
    if first in (b"+", b"-", b":", b",", b"(", b"#", b"_"):
        return raw
    if first in (b"$", b"!", b"="):
        size = int(line[:-2])
        if size < 0:
            return raw
        body = stream.read(size + 2)
        if len(body) != size + 2:
            raise ConnectionError("Redis upstream returned a truncated bulk reply")
        return raw + body
    if first in (b"*", b"~", b">", b"%", b"|"):
        count = int(line[:-2])
        if count < 0:
            return raw
        for _ in range(count * (2 if first in (b"%", b"|") else 1)):
            raw += _read_resp_reply(stream)
        return raw
    raise ValueError("Redis proxy received an unsupported response type")


def _is_target_attachment_append(args: list[bytes]) -> bool:
    return (
        len(args) >= 8
        and args[0].upper() == b"EVAL"
        and b"XADD" in args[1]
        and args[7] == b"task.attachment.linked"
    )


class _RelayFaultServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, upstream, faults):
        self.upstream = upstream
        self.faults = faults
        super().__init__(address, _RelayFaultHandler)


class _RelayFaultHandler(socketserver.BaseRequestHandler):
    def handle(self):
        parsed = self.server.upstream
        try:
            upstream = socket.create_connection(
                (parsed.hostname, parsed.port or 6379), timeout=5
            )
            upstream.settimeout(15)
        except OSError:
            self.request.sendall(
                b"-ERR attachment relay proxy upstream unavailable\r\n"
            )
            return
        with (
            upstream,
            self.request.makefile("rb") as client_stream,
            upstream.makefile("rb") as upstream_stream,
        ):
            while True:
                try:
                    args, raw = _read_resp_command(client_stream)
                    if not raw:
                        return
                    if _is_target_attachment_append(args):
                        self.server.faults.append("task.attachment.linked")
                        self.request.sendall(
                            b"-ERR isolated task attachment relay fault\r\n"
                        )
                        continue
                    upstream.sendall(raw)
                    response = _read_resp_reply(upstream_stream)
                    self.request.sendall(response)
                except (ConnectionError, OSError, ValueError):
                    return


def _relay_fault_proxy(redis_url: str, faults: list[str]):
    upstream = urlsplit(redis_url)
    if upstream.scheme != "redis" or upstream.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise RuntimeError("isolated relay proxy requires a local redis:// URL")
    server = _RelayFaultServer(("127.0.0.1", 0), upstream, faults)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    userinfo = upstream.netloc.rsplit("@", 1)[0] + "@" if "@" in upstream.netloc else ""
    proxy_url = urlunsplit(
        (
            "redis",
            f"{userinfo}127.0.0.1:{server.server_address[1]}",
            upstream.path,
            upstream.query,
            upstream.fragment,
        )
    )
    return server, thread, proxy_url


def installed_task_attachment_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Run ordinary installed upload/add/list/export and read-only controls."""
    root = cwd / f"task-attachment-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    workspace = root / "workspace"
    caller = root / "caller"
    fixtures = root / "fixtures"
    logs = root / "streams"
    for directory in (workspace, caller, fixtures, logs):
        directory.mkdir(mode=0o700)
    database = workspace / ".aig" / "store.db"
    identity = workspace / ".aig" / "identity"
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env.pop("PYTHONHOME", None)
    for credential in (
        "AIGINEERING_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        local_env.pop(credential, None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")
    commands: list[dict] = []

    def run(
        phase: str,
        argv: list[str],
        *,
        expected: int = 0,
        parse: bool = True,
        command_env: dict[str, str] | None = None,
    ):
        ordinal = len(commands) + 1
        proc = subprocess.Popen(
            argv,
            cwd=caller,
            env=command_env or local_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or b""
            err = exc.stderr or b""
            if isinstance(out, str):
                out = out.encode()
            if isinstance(err, str):
                err = err.encode()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            tail_out, tail_err = proc.communicate(timeout=10)
            out = tail_out or out
            err = tail_err or err
            _write(logs / f"{ordinal:03}-{phase}.stdout.raw", out)
            _write(logs / f"{ordinal:03}-{phase}.stderr.raw", err)
            commands.append(
                {
                    "phase": phase,
                    "argv": argv,
                    "exit": None,
                    "timeout": True,
                    "stdout_path": f"{ordinal:03}-{phase}.stdout.raw",
                    "stderr_path": f"{ordinal:03}-{phase}.stderr.raw",
                    "stdout_sha256": _hash(out),
                    "stderr_sha256": _hash(err),
                }
            )
            raise RuntimeError(
                f"{phase} timed out; private byte streams retained"
            ) from None
        _write(logs / f"{ordinal:03}-{phase}.stdout.raw", stdout)
        _write(logs / f"{ordinal:03}-{phase}.stderr.raw", stderr)
        commands.append(
            {
                "phase": phase,
                "argv": argv,
                "exit": proc.returncode,
                "stdout_path": f"{ordinal:03}-{phase}.stdout.raw",
                "stderr_path": f"{ordinal:03}-{phase}.stderr.raw",
                "stdout_sha256": _hash(stdout),
                "stderr_sha256": _hash(stderr),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            }
        )
        if proc.returncode != expected:
            raise RuntimeError(
                f"{phase} exited {proc.returncode}; inspect retained private streams"
            )
        result = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        if any(marker.encode() in stdout + stderr for marker in _MARKERS):
            raise RuntimeError(
                f"{phase} wrote a private fixture marker; raw streams retained"
            )
        if not parse:
            return result, None
        try:
            payload = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(
                f"{phase} did not return JSON; inspect retained streams"
            ) from None
        return result, payload

    def cli(
        phase: str,
        *args: str,
        expected: int = 0,
        append_json: bool = True,
        parse_json: bool = True,
    ):
        argv = [str(aig), "-C", str(workspace), *args]
        if append_json:
            argv.append("--json")
        return run(phase, argv, expected=expected, parse=parse_json)

    def attach(
        phase: str,
        action: str,
        *args: str,
        expected: int = 0,
        command_env: dict[str, str] | None = None,
    ):
        return run(
            phase,
            [
                str(aig),
                "task",
                "attachment",
                action,
                *args,
                "--store",
                str(database),
                "--json",
            ],
            expected=expected,
            command_env=command_env,
        )

    def fingerprint(phase: str) -> dict:
        _, value = run(phase, [str(python), "-c", _FINGERPRINT, str(database)])
        if not isinstance(value, dict) or not isinstance(value.get("tables"), dict):
            raise RuntimeError(f"{phase} did not return table fingerprints")
        if set(value["tables"]) != _TABLES:
            raise RuntimeError(f"{phase} did not fingerprint the exact 18-table schema")
        return value

    def read_command(phase: str, action: str, *args: str, expected: int = 0):
        before = fingerprint(f"{phase}-before")
        completed, payload = attach(phase, action, *args, expected=expected)
        after = fingerprint(f"{phase}-after")
        if before != after:
            raise RuntimeError(f"{phase} changed custom Store tables")
        if expected == 0 and isinstance(payload, dict) and payload.get("ok") is True:
            revision = payload.get("revision")
            if type(revision) is not int or revision != before.get("revision"):
                raise RuntimeError(
                    f"{phase} revision does not match its read-only snapshot"
                )
        if any(
            marker.encode() in completed.stdout + completed.stderr
            for marker in _MARKERS
        ):
            raise RuntimeError(f"{phase} exposed private fixture content")
        return completed, payload

    def read_cli(phase: str, *args: str, expect_revision: bool = True):
        before = fingerprint(f"{phase}-before")
        completed, payload = cli(phase, *args)
        after = fingerprint(f"{phase}-after")
        if before != after:
            raise RuntimeError(f"{phase} changed custom Store tables")
        if expect_revision and isinstance(payload, dict) and payload.get("ok") is True:
            revision = payload.get("revision")
            if type(revision) is not int or revision != before.get("revision"):
                raise RuntimeError(f"{phase} revision differs from its SQLite snapshot")
        return completed, payload

    def read_status(phase: str, contract_id: str):
        completed, payload = read_cli(
            phase, "task", "status", contract_id, expect_revision=False
        )
        try:
            _validate_flat_task_status(payload, contract_id)
        except RuntimeError as exc:
            raise RuntimeError(
                f"{phase} did not return the flat task-status shape"
            ) from exc
        return completed, payload

    def require_error(phase: str, payload: object, code: str) -> None:
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not False
            or set(payload) != {"ok", "error"}
            or payload.get("error") != code
        ):
            raise RuntimeError(f"{phase} returned a noncanonical safe error envelope")

    try:
        # The CLI and Python must come from the same installed environment.
        _, origin = run(
            "installed-origin",
            [
                str(python),
                "-c",
                "import hashlib,json,pathlib,sys,aigineering,aigineering.plugins.task_attachments as p;print(json.dumps({'prefix':str(pathlib.Path(sys.prefix).absolute()),'module':str(pathlib.Path(aigineering.__file__).absolute()),'module_sha256':hashlib.sha256(pathlib.Path(aigineering.__file__).read_bytes()).hexdigest(),'plugin':str(pathlib.Path(p.__file__).absolute()),'plugin_sha256':hashlib.sha256(pathlib.Path(p.__file__).read_bytes()).hexdigest()}))",
            ],
        )
        module = Path(origin["module"]).resolve()
        prefix = Path(origin["prefix"]).resolve()
        if (
            prefix != python.parent.parent.resolve()
            or aig.parent.resolve() != prefix / "bin"
            or not module.is_relative_to(prefix)
            or not Path(origin["plugin"]).resolve().is_relative_to(prefix)
            or "site-packages" not in str(module)
        ):
            raise RuntimeError(
                "aig/Python are not bound to one source-external install"
            )

        cli("domain-init", "domain", "init")
        if not database.is_file():
            raise RuntimeError("fixture domain did not create its Store")
        body = b"\x00binary\xff\n" + _MARKERS[0].encode() + b"\n"
        source = fixtures / "payload.bin"
        _write(source, body)
        _, imported = cli(
            "artifact-import",
            "artifact",
            "import",
            str(source),
            "--name",
            "attachment-private",
            "--media-type",
            "application/octet-stream",
            "--source-uri",
            _MARKERS[1],
            append_json=False,
            parse_json=True,
        )
        asset_id = imported.get("id") if isinstance(imported, dict) else None
        if not isinstance(asset_id, str) or not asset_id:
            raise RuntimeError("ordinary signed artifact import returned no Asset ID")
        _, task = cli(
            "task-create",
            "task",
            "create",
            "--name",
            "attachment-target",
            "--output",
            "attachment-result",
        )
        contract_id = task.get("contract_id") if isinstance(task, dict) else None
        if not isinstance(contract_id, str) or not contract_id:
            raise RuntimeError("ordinary signed task creation returned no Contract ID")
        _, status_before = read_status("status-before-attachment", contract_id)
        _, context_before = read_cli(
            "context-before-attachment", "task", "context", contract_id
        )

        add_args = (contract_id, asset_id)
        first = attach("add-default", "add", *add_args)[1]
        if (
            not isinstance(first, dict)
            or first.get("ok") is not True
            or set(first) != {"ok", "data", "revision"}
        ):
            raise RuntimeError(
                "attachment add did not return its exact success envelope"
            )
        first_data = first["data"]
        if (
            set(first_data)
            != {
                "contract_id",
                "asset_id",
                "record_id",
                "candidate_id",
                "asset_candidate_id",
                "idempotency_key",
                "fact_ids",
            }
            or type(first.get("revision")) is not int
            or first_data.get("contract_id") != contract_id
            or first_data.get("asset_id") != asset_id
            or first_data.get("fact_ids") != [first_data.get("record_id")]
            or first_data.get("idempotency_key")
            != f"task-attachment:{contract_id}:{asset_id}"
            or not all(
                isinstance(first_data.get(key), str) and first_data[key]
                for key in (
                    "record_id",
                    "candidate_id",
                    "asset_candidate_id",
                    "idempotency_key",
                )
            )
        ):
            raise RuntimeError(
                "add response does not bind the exact task/Asset relation"
            )
        before_replay = fingerprint("default-replay-before")
        replay = attach("add-default-replay", "add", *add_args)[1]
        after_replay = fingerprint("default-replay-after")
        if replay.get("data") != first_data:
            raise RuntimeError(
                "default pair replay did not return its original Candidate/link"
            )
        if before_replay != after_replay:
            raise RuntimeError(
                "exact default replay changed relation or materialized Store"
            )
        _, status_after = read_status("status-after-attachment", contract_id)
        _assert_same_task_state(status_before, status_after, contract_id)
        _, context_after = read_cli(
            "context-after-attachment", "task", "context", contract_id
        )
        if context_before.get("data") != context_after.get("data"):
            raise RuntimeError(
                "ordinary attachment association changed frozen task context"
            )

        second_source = fixtures / "second-payload.bin"
        _write(second_source, body + b"second immutable artifact\n")
        _, second_import = cli(
            "second-artifact-import",
            "artifact",
            "import",
            str(second_source),
            "--name",
            "attachment-private-second",
            "--media-type",
            "application/octet-stream",
            "--source-uri",
            _MARKERS[1],
            append_json=False,
            parse_json=True,
        )
        second_asset_id = second_import["id"]

        foreign_workspace = root / "foreign-workspace"
        foreign_workspace.mkdir(mode=0o700)
        foreign_env = dict(local_env)
        foreign_env["AIG_ACTOR_KEY_FILE"] = str(
            foreign_workspace / ".aig" / "identity" / "root.ed25519"
        )
        run(
            "foreign-domain-init",
            [str(aig), "-C", str(foreign_workspace), "domain", "init", "--json"],
            command_env=foreign_env,
        )
        foreign_source = fixtures / "foreign.bin"
        _write(foreign_source, b"foreign-domain attachment fixture")
        _, foreign_import = run(
            "foreign-domain-asset-import",
            [
                str(aig),
                "-C",
                str(foreign_workspace),
                "artifact",
                "import",
                str(foreign_source),
                "--name",
                "foreign-attachment",
                "--media-type",
                "application/octet-stream",
            ],
            command_env=foreign_env,
        )
        foreign_asset_id = foreign_import.get("id")
        if not isinstance(foreign_asset_id, str) or not foreign_asset_id:
            raise RuntimeError("foreign-domain signed Asset publication returned no ID")
        foreign_before = fingerprint("foreign-domain-add-before")
        foreign_rejected = attach(
            "reject-foreign-domain-asset",
            "add",
            contract_id,
            foreign_asset_id,
            expected=1,
        )[1]
        if foreign_before != fingerprint("foreign-domain-add-after"):
            raise RuntimeError("foreign-domain refusal wrote into the selected Store")
        require_error(
            "reject-foreign-domain-asset", foreign_rejected, "asset_not_found"
        )

        invalid_asset_ids = {}
        for variant in ("sealed", "bad_hash", "wrong_kind"):
            _, variant_result = run(
                f"signed-{variant}-asset-publication",
                [
                    str(python),
                    "-c",
                    _PUBLISH_VARIANT,
                    variant,
                    f"attachment-{variant}",
                    str(database),
                ],
            )
            asset_variant_id = variant_result.get("asset_id")
            if not isinstance(asset_variant_id, str) or not asset_variant_id:
                raise RuntimeError(f"signed {variant} fixture returned no Asset ID")
            invalid_asset_ids[variant] = asset_variant_id
            rejected = attach(
                f"reject-{variant}-asset",
                "add",
                contract_id,
                asset_variant_id,
                "--idempotency-key",
                f"installed-attachment-invalid-{variant}",
                expected=1,
            )[1]
            require_error(f"reject-{variant}-asset", rejected, "candidate_rejected")
        explicit_key = "installed-attachment-explicit-v1"
        keyed = attach(
            "add-explicit-key",
            "add",
            contract_id,
            asset_id,
            "--idempotency-key",
            explicit_key,
        )[1]
        before_keyed_replay = fingerprint("explicit-key-replay-before")
        keyed_replay = attach(
            "add-explicit-key-replay",
            "add",
            contract_id,
            asset_id,
            "--idempotency-key",
            explicit_key,
        )[1]
        if keyed_replay.get("data") != keyed.get("data"):
            raise RuntimeError("explicit idempotency replay changed relation identity")
        if (
            keyed.get("data", {}).get("idempotency_key") != explicit_key
            or keyed.get("data", {}).get("record_id") == first_data.get("record_id")
            or keyed.get("data", {}).get("candidate_id")
            == first_data.get("candidate_id")
        ):
            raise RuntimeError(
                "explicit key did not create a separately attributable relation"
            )
        if before_keyed_replay != fingerprint("explicit-key-replay-after"):
            raise RuntimeError("explicit exact replay appended duplicate durable facts")
        conflict = attach(
            "idempotency-cross-pair-rejection",
            "add",
            contract_id,
            second_asset_id,
            "--idempotency-key",
            explicit_key,
            expected=1,
        )[1]
        require_error(
            "idempotency-cross-pair-rejection", conflict, "candidate_rejected"
        )

        redis_url = local_env.get("AIGINEERING_REDIS_URL", "")
        relay_faults: list[str] = []
        proxy, proxy_thread, proxy_url = _relay_fault_proxy(redis_url, relay_faults)
        proxy_env = dict(local_env)
        proxy_env["AIGINEERING_REDIS_URL"] = proxy_url
        relay_key = "installed-attachment-relay-pending-v1"
        before_pending = fingerprint("postcommit-pending-before")
        try:
            pending = attach(
                "postcommit-relay-pending",
                "add",
                contract_id,
                second_asset_id,
                "--idempotency-key",
                relay_key,
                expected=1,
                command_env=proxy_env,
            )[1]
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=5)
        if relay_faults != ["task.attachment.linked"]:
            raise RuntimeError("relay proxy did not fault exactly the linked fact")
        if (
            not isinstance(pending, dict)
            or set(pending)
            != {
                "ok",
                "error",
                "candidate_accepted",
                "facts_retained",
                "data",
                "revision",
            }
            or pending.get("ok") is not False
            or pending.get("error") != "redis_sync_pending"
            or pending.get("candidate_accepted") is not True
            or pending.get("facts_retained") is not True
            or not isinstance(pending.get("data"), dict)
            or type(pending.get("revision")) is not int
        ):
            raise RuntimeError("postcommit relay failure lost accepted result data")
        pending_data = pending["data"]
        if (
            set(pending_data)
            != {
                "contract_id",
                "asset_id",
                "record_id",
                "candidate_id",
                "asset_candidate_id",
                "idempotency_key",
                "fact_ids",
            }
            or not isinstance(pending_data.get("record_id"), str)
            or not isinstance(pending_data.get("candidate_id"), str)
            or pending_data.get("contract_id") != contract_id
            or pending_data.get("asset_id") != second_asset_id
            or pending_data.get("idempotency_key") != relay_key
            or pending_data.get("fact_ids") != [pending_data.get("record_id")]
        ):
            raise RuntimeError("pending result does not bind its durable relation")
        pending_fingerprint = fingerprint("postcommit-pending-store")
        if (
            pending_fingerprint["revision"] <= before_pending["revision"]
            or pending_fingerprint["tables"]["runtime_records"]["count"]
            <= before_pending["tables"]["runtime_records"]["count"]
        ):
            raise RuntimeError(
                "relay-pending response was not independently reflected in durable history"
            )
        durable_completed, durable_payload = run(
            "postcommit-pending-exact-durable-proof",
            [
                str(python),
                "-c",
                _PROVE_DURABLE_LINK,
                str(database),
                contract_id,
                pending_data["record_id"],
                second_asset_id,
                pending_data["candidate_id"],
                pending_data["asset_candidate_id"],
                relay_key,
            ],
        )
        if (
            not isinstance(durable_payload, dict)
            or durable_payload.get("record_id") != pending_data["record_id"]
            or durable_payload.get("record_type") != "task.attachment.linked"
            or durable_payload.get("candidate_id") != pending_data["candidate_id"]
            or durable_payload.get("asset_candidate_id")
            != pending_data["asset_candidate_id"]
            or durable_payload.get("revision") != pending_fingerprint["revision"]
            or durable_payload.get("record_count")
            != pending_fingerprint["tables"]["runtime_records"]["count"]
            or durable_completed.returncode != 0
        ):
            raise RuntimeError(
                "exact accepted link was not durable before its replay catch-up"
            )
        caught_up = attach(
            "postcommit-exact-replay-catchup",
            "add",
            contract_id,
            second_asset_id,
            "--idempotency-key",
            relay_key,
        )[1]
        caught_up_fingerprint = fingerprint("postcommit-catchup-store")
        if (
            caught_up.get("data") != pending_data
            or pending_fingerprint != caught_up_fingerprint
        ):
            raise RuntimeError(
                "exact replay failed to relay retained facts without duplication"
            )

        listed = read_command("list", "list", contract_id, "--limit", "1")[1]
        if (
            not isinstance(listed, dict)
            or listed.get("ok") is not True
            or set(listed) != {"ok", "data", "revision"}
            or type(listed.get("revision")) is not int
        ):
            raise RuntimeError("list response envelope is malformed")
        data = listed["data"]
        rows = data.get("attachments")
        if (
            set(data)
            != {
                "contract_id",
                "domain_id",
                "watermark",
                "attachments",
                "next_cursor",
                "truncated",
            }
            or type(data.get("watermark")) is not int
        ):
            raise RuntimeError("list returned unexpected top-level data fields")
        allow = {
            "record_id",
            "revision",
            "recorded_at",
            "contract_id",
            "asset_id",
            "candidate_id",
            "actor_id",
            "key_id",
            "asset_candidate_id",
            "name",
            "media_type",
            "size",
            "sha256",
        }
        if not isinstance(rows, list) or len(rows) != 1 or set(rows[0]) != allow:
            raise RuntimeError("list metadata differs from the exact safe allowlist")
        if (
            rows[0].get("contract_id") != contract_id
            or rows[0].get("size") != len(body) + len(b"second immutable artifact\n")
            or rows[0].get("sha256") != _hash(body + b"second immutable artifact\n")
            or rows[0].get("name") != "attachment-private-second"
            or rows[0].get("media_type") != "application/octet-stream"
            or type(rows[0].get("revision")) is not int
            or not all(
                isinstance(rows[0].get(key), str) and rows[0][key]
                for key in (
                    "record_id",
                    "candidate_id",
                    "asset_id",
                    "actor_id",
                    "key_id",
                    "asset_candidate_id",
                    "name",
                    "media_type",
                )
            )
        ):
            raise RuntimeError(
                "attachment row metadata is not bound to a typed exact link"
            )
        if data.get("truncated") is not True or not isinstance(
            data.get("next_cursor"), str
        ):
            raise RuntimeError("first list page did not expose its stable cursor")
        public = json.dumps(listed, sort_keys=True, ensure_ascii=True)
        if (
            any(marker in public for marker in _MARKERS)
            or "content" in public
            or "source_uri" in public
        ):
            raise RuntimeError("list disclosed body, source URI, or private marker")
        cursor = data.get("next_cursor")
        if not isinstance(cursor, str) or not cursor:
            raise RuntimeError(
                "first attachment page did not expose a continuation cursor"
            )
        second_page = read_command(
            "list-second-page", "list", contract_id, "--limit", "1", "--cursor", cursor
        )[1]
        second_rows = second_page.get("data", {}).get("attachments")
        if (
            not isinstance(second_rows, list)
            or len(second_rows) != 1
            or second_rows[0].get("record_id") == rows[0].get("record_id")
            or set(second_rows[0]) != allow
        ):
            raise RuntimeError("cursor page repeated or omitted an attachment relation")
        third_page = read_command(
            "list-third-page",
            "list",
            contract_id,
            "--limit",
            "1",
            "--cursor",
            second_page["data"]["next_cursor"],
        )[1]
        third_rows = third_page.get("data", {}).get("attachments")
        if (
            not isinstance(third_rows, list)
            or len(third_rows) != 1
            or third_rows[0].get("record_id")
            in {rows[0].get("record_id"), second_rows[0].get("record_id")}
            or third_page["data"].get("truncated") is not False
            or third_page["data"].get("next_cursor") is not None
        ):
            raise RuntimeError("final cursor page repeated data or failed to terminate")
        all_page_ids = {
            rows[0]["record_id"],
            second_rows[0]["record_id"],
            third_rows[0]["record_id"],
        }
        if all_page_ids != {
            first_data["record_id"],
            keyed["data"]["record_id"],
            pending_data["record_id"],
        }:
            raise RuntimeError("pagination omitted or introduced a signed relation")
        human_before = fingerprint("human-list-before")
        human_result, human_payload = run(
            "human-list-output",
            [
                str(aig),
                "task",
                "attachment",
                "list",
                contract_id,
                "--store",
                str(database),
            ],
        )
        if human_before != fingerprint("human-list-after"):
            raise RuntimeError("human list output changed custom Store tables")
        human_rows = human_payload.get("data", {}).get("attachments")
        if (
            not isinstance(human_rows, list)
            or {row.get("record_id") for row in human_rows} != all_page_ids
            or any(marker in human_result.stdout.decode("utf-8") for marker in _MARKERS)
            or any("content" in row or "source_uri" in row for row in human_rows)
        ):
            raise RuntimeError(
                "human list output exposed non-allowlisted attachment data"
            )
        bad_cursor = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        bad_page = read_command(
            "list-corrupt-cursor",
            "list",
            contract_id,
            "--limit",
            "1",
            "--cursor",
            bad_cursor,
            expected=1,
        )[1]
        require_error("list-corrupt-cursor", bad_page, "invalid_cursor")

        # An unlinked task cannot export the otherwise valid Asset.
        _, other = cli(
            "second-task-create",
            "task",
            "create",
            "--name",
            "attachment-unlinked",
            "--output",
            "other-result",
        )
        other_id = other["contract_id"]
        foreign_cursor = read_command(
            "list-foreign-task-cursor",
            "list",
            other_id,
            "--limit",
            "1",
            "--cursor",
            cursor,
            expected=1,
        )[1]
        require_error(
            "list-foreign-task-cursor", foreign_cursor, "cursor_scope_mismatch"
        )
        destination = root / "must-not-exist.bin"
        error = read_command(
            "unlinked-export",
            "export",
            other_id,
            asset_id,
            "--output",
            str(destination),
            expected=1,
        )[1]
        require_error("unlinked-export", error, "attachment_not_linked")
        if destination.exists():
            raise RuntimeError("unlinked export created a destination")

        # A terminal task may receive operator metadata without reopening.
        cli(
            "cancel-secondary-task",
            "task",
            "cancel",
            other_id,
            "--reason",
            "installed attachment fixture terminal state",
        )
        canceled_before = read_status("canceled-status-before-link", other_id)[1]
        if canceled_before["status"] != "cancelled":
            raise RuntimeError("fixture task did not reach the expected terminal state")
        terminal_link = attach(
            "link-canceled-task",
            "add",
            other_id,
            second_asset_id,
            "--idempotency-key",
            "installed-attachment-canceled-task-v1",
        )[1]
        if terminal_link.get("ok") is not True:
            raise RuntimeError("attachment association to a canceled task failed")
        canceled_after = read_status("canceled-status-after-link", other_id)[1]
        _assert_same_task_state(canceled_before, canceled_after, other_id)
        if canceled_after["status"] != "cancelled":
            raise RuntimeError("attachment association reopened a canceled task")

        output = root / "exported.bin"
        exported = read_command(
            "export", "export", contract_id, asset_id, "--output", str(output)
        )[1]
        if (
            exported.get("ok") is not True
            or set(exported) != {"ok", "data", "revision"}
            or set(exported["data"])
            != {"contract_id", "asset_id", "output_path", "sha256", "size"}
            or exported["data"].get("contract_id") != contract_id
            or exported["data"].get("asset_id") != asset_id
            or exported["data"].get("sha256") != _hash(body)
            or exported["data"].get("size") != len(body)
        ):
            raise RuntimeError("export metadata does not bind exact source bytes")
        if output.read_bytes() != body or stat.S_IMODE(output.stat().st_mode) != 0o600:
            raise RuntimeError("export bytes or exclusive private mode differ")
        sentinel = root / "sentinel.bin"
        _write(sentinel, b"preserve-me")
        existing = read_command(
            "existing-destination",
            "export",
            contract_id,
            asset_id,
            "--output",
            str(sentinel),
            expected=1,
        )[1]
        require_error("existing-destination", existing, "destination_exists")
        if sentinel.read_bytes() != b"preserve-me":
            raise RuntimeError("existing destination was overwritten")

        symlink_target = root / "symlink-target.bin"
        _write(symlink_target, b"symlink-target-preserved")
        symlink_path = root / "symlink-export.bin"
        symlink_path.symlink_to(symlink_target)
        symlink_error = read_command(
            "symlink-destination",
            "export",
            contract_id,
            asset_id,
            "--output",
            str(symlink_path),
            expected=1,
        )[1]
        require_error("symlink-destination", symlink_error, "destination_exists")
        if (
            not symlink_path.is_symlink()
            or symlink_target.read_bytes() != b"symlink-target-preserved"
        ):
            raise RuntimeError("symlink destination was followed or changed")

        # No implicit fallback to caller/.aig/store.db or creation of a missing Store.
        default_store = caller / ".aig" / "store.db"
        missing = caller / "missing.db"
        wrong_before = fingerprint("missing-store-before")
        wrong = run(
            "missing-store-no-fallback",
            [
                str(aig),
                "task",
                "attachment",
                "list",
                contract_id,
                "--store",
                str(missing),
                "--json",
            ],
            expected=1,
        )[1]
        if wrong_before != fingerprint("missing-store-after"):
            raise RuntimeError("missing Store lookup changed the configured Store")
        require_error("missing-store-no-fallback", wrong, "store_unavailable")
        if default_store.exists() or missing.exists():
            raise RuntimeError(
                "attachment CLI created or fell back to an unintended Store"
            )
        omitted_before = fingerprint("omitted-store-before")
        omitted = run(
            "omitted-store-no-fallback",
            [str(aig), "task", "attachment", "list", contract_id, "--json"],
            expected=1,
        )[1]
        if omitted_before != fingerprint("omitted-store-after"):
            raise RuntimeError("omitted Store lookup changed the configured Store")
        require_error("omitted-store-no-fallback", omitted, "store_unavailable")
        if default_store.exists():
            raise RuntimeError("omitted --store created a caller default Store")
        table_count = len(fingerprint("final-custom-store-fingerprint")["tables"])
        if table_count != 18:
            raise RuntimeError("custom Store fingerprint did not cover all 18 tables")
        rebuild = run(
            "reopen-rebuild-identity",
            [
                str(python),
                "-c",
                "import json,sys;from aigineering.core.sqlite_store import SQLiteStore;"
                "p=sys.argv[1];s=SQLiteStore(p);"
                "records=lambda x:sorted((r.id,r.recorded_at) for _,r in x.scan_runtime_records());"
                "before=records(s);d0=s.runtime_materialization_digest();d1=s.rebuild_runtime_materializations();"
                "after=records(s);s.close();s=SQLiteStore(p);reopened=records(s);"
                "d2=s.runtime_materialization_digest();d3=s.rebuild_runtime_materializations();final=records(s);s.close();"
                "print(json.dumps({'same_records_after_rebuild':before==after,'same_records_after_reopen':after==reopened,'same_records_after_second_rebuild':reopened==final,'stable_materialization_digest':d0==d1==d2==d3,'record_count':len(before)}))",
                str(database),
            ],
        )[1]
        if not isinstance(rebuild, dict) or not all(
            rebuild.get(key) is True
            for key in (
                "same_records_after_rebuild",
                "same_records_after_reopen",
                "same_records_after_second_rebuild",
                "stable_materialization_digest",
            )
        ):
            raise RuntimeError(
                "SQLite close/reopen/materialization rebuild changed immutable facts"
            )
        rebuilt_listing = read_command(
            "list-after-rebuild", "list", contract_id, "--limit", "1"
        )[1]
        if rebuilt_listing.get("data", {}).get("attachments") != listed["data"].get(
            "attachments"
        ):
            raise RuntimeError("materialization rebuild changed attachment listing")

        # Signed actor revocation exercises current authorization without
        # altering the already committed attachment relation.
        listed_row = rows[0]
        _, revoke = run(
            "signed-attachment-key-revocation",
            [
                str(python),
                "-c",
                _REVOKE_KEY,
                listed_row["actor_id"],
                listed_row["key_id"],
                str(database),
            ],
        )
        if not isinstance(revoke, dict) or revoke.get("accepted") is not True:
            raise RuntimeError("ordinary signed actor-key revocation was not accepted")
        revoked_before = fingerprint("revoked-replay-before")
        revoked_replay = attach(
            "revoked-exact-replay", "add", contract_id, asset_id, expected=1
        )[1]
        revoked_after = fingerprint("revoked-replay-after")
        if (
            not isinstance(revoked_replay, dict)
            or revoked_replay.get("ok") is not False
            or set(revoked_replay) != {"ok", "error"}
            or revoked_replay.get("error") != "authorization_unavailable"
        ):
            raise RuntimeError("revoked-key replay did not fail with a safe refusal")
        if revoked_before != revoked_after:
            raise RuntimeError(
                "prepublication revoked-key refusal wrote a Candidate or fact"
            )
        after_revocation = read_command("list-after-revocation", "list", contract_id)[1]
        after_ids = {
            row["record_id"] for row in after_revocation["data"]["attachments"]
        }
        if all_page_ids != after_ids:
            raise RuntimeError("revoked-key replay changed attachment relation history")
        public_command_results = [
            {
                key: row[key]
                for key in (
                    "phase",
                    "exit",
                    "timeout",
                    "stdout_sha256",
                    "stderr_sha256",
                    "stdout_bytes",
                    "stderr_bytes",
                )
                if key in row
            }
            for row in commands
        ]
        manifest = {
            "schema": "aig-installed-task-attachment-smoke-v1",
            "status": "passed",
            "commands": commands,
            "contract_id": contract_id,
            "asset_id": asset_id,
            "second_asset_id": second_asset_id,
            "foreign_asset_id": foreign_asset_id,
            "invalid_asset_ids": invalid_asset_ids,
            "record_id": first_data["record_id"],
            "explicit_record_id": keyed["data"]["record_id"],
            "table_count": table_count,
            "rebuild": rebuild,
            "relay_fault_records": relay_faults,
            "relay_fault_model": "loopback proxy rejected only the linked-fact EVAL after forwarding other Redis operations",
            "actual_redis_outage": False,
            "installed_module_sha256": origin["module_sha256"],
            "installed_plugin_sha256": origin["plugin_sha256"],
        }
        _write(
            logs / "author-manifest.json",
            (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        )
        return {
            "status": "passed",
            "scope": "installed CLI signed upload/add/list/export; custom Store; exact bytes; read neutrality",
            "contract_id": contract_id,
            "asset_id": asset_id,
            "record_id": first_data["record_id"],
            "command_count": len(commands),
            "sqlite_table_count": table_count,
            "commands": public_command_results,
            "untested": [
                "tombstone publication and forced partial filesystem write have no ordinary installed publisher seam",
                "complete task lifecycle-state matrix and forged historical fact injection remain source-test controls",
                "real provider/Worker execution is outside this local attachment workflow",
            ],
        }
    except Exception:
        # Preserve a phase manifest without exposing fixture contents.
        manifest = logs / "author-failure.json"
        _write(
            manifest,
            (json.dumps({"commands": commands}, sort_keys=True) + "\n").encode(),
        )
        raise
