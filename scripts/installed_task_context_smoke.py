"""Installed-CLI smoke for the read-only, task-scoped context inspector.

The helper deliberately imports no project modules. Setup uses the installed
CLI and its normal signed Candidate publication owner; context queries run in
separate installed CLI processes. Raw streams and the fixture Store stay in a
private caller-owned evidence directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from urllib.parse import urlsplit


_PRIVATE_MARKERS = (
    "AIG_PRIVATE_CONTEXT_INPUT_20261007",
    "AIG_PRIVATE_CONTEXT_REDACTED_20261007",
    "AIG_PRIVATE_CONTEXT_UNRELATED_20261007",
    "AIG_PRIVATE_CONTEXT_HIDDEN_20261007",
    "AIG_PRIVATE_CONTEXT_DESCRIPTION_20261007",
)
_REDACTED = "[redacted]"
_REDIS_PREFLIGHT_MESSAGE = (
    "Redis collaboration service unavailable; check Redis installation and health"
)
_METADATA_KEYS = {
    "id",
    "name",
    "content_type",
    "origin",
    "trust_tier",
    "promptable",
    "disclosure_view",
    "tombstoned",
}

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
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str).encode()
        tables[name] = {"row_count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    revision = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM runtime_records").fetchone()[0]
    encoded = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode()
    print(json.dumps({
        "revision": int(revision),
        "table_digest": hashlib.sha256(encoded).hexdigest(),
        "tables": tables,
    }, sort_keys=True))
finally:
    conn.close()
"""

_REOPEN_REBUILD = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore

path = sys.argv[1]
def records(store):
    return sorted((record.id, record.recorded_at) for _, record in store.scan_runtime_records())

store = SQLiteStore(path)
before = records(store)
first_digest = store.rebuild_runtime_materializations()
after = records(store)
store.close()
store = SQLiteStore(path)
reopened = records(store)
second_digest = store.rebuild_runtime_materializations()
final = records(store)
store.close()
print(json.dumps({
    "same_records_after_rebuild": before == after,
    "same_records_after_reopen": after == reopened,
    "same_records_after_second_rebuild": reopened == final,
    "stable_rebuild_digest": first_digest == second_digest,
    "record_count": len(before),
}, sort_keys=True))
"""

_PUBLISH_REDACTED_ASSET = r"""
import json, sys
from dataclasses import replace
from pathlib import Path
from aigineering.cli._common import _persistent_store
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.local_publication import commit_local_effect, require_accepted
from aigineering.protocol.effect_builders import asset_proposal_effect

name, content_path = sys.argv[1], Path(sys.argv[2])
asset = build_control_plane_asset(
    name=name,
    content=content_path.read_text(encoding="utf-8"),
    origin="human",
    trust_tier="human",
    source_uri="",
    promptable=True,
)
asset = replace(asset, disclosure_view="redacted")
store = _persistent_store()
try:
    decision = require_accepted(commit_local_effect(
        store,
        asset_proposal_effect(asset),
        idempotency_key="installed-task-context-redacted-asset-v1",
    ))
    print(json.dumps({"id": decision.assets[0].id, "name": decision.assets[0].name}))
finally:
    store.close()
"""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _assert_private_stderr_absent(phase: str, stderr: str) -> None:
    if any(marker in stderr for marker in _PRIVATE_MARKERS):
        raise RuntimeError(
            f"{phase} wrote private content to stderr; inspect retained private streams"
        )


def _assert_success_envelope_revision(
    phase: str, payload: object, expected_revision: int
) -> None:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return
    revision = payload.get("revision")
    if (
        set(payload) != {"ok", "data", "revision"}
        or not isinstance(payload.get("data"), dict)
        or type(expected_revision) is not int
        or type(revision) is not int
        or revision != expected_revision
    ):
        raise RuntimeError(
            f"{phase} success envelope revision does not match the SQLite snapshot"
        )


def _assert_error_envelope(
    phase: str,
    payload: object,
    code: str,
    *,
    allow_redis_preflight_message: bool = False,
) -> None:
    expected = {"ok": False, "error": code}
    if allow_redis_preflight_message:
        if phase != "context-required-redis-outage" or code != "redis_unavailable":
            raise RuntimeError(
                f"{phase} cannot use the Redis preflight error allowance"
            )
        expected["message"] = _REDIS_PREFLIGHT_MESSAGE
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not False
        or set(payload) != set(expected)
        or payload != expected
    ):
        raise RuntimeError(f"{phase} did not return the exact safe error envelope")


def _write_private(path: Path, content: str | bytes) -> None:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def _replace_manifest(path: Path, rows: list[dict]) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    raw = (
        json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    _write_private(temporary, raw)
    os.replace(temporary, path)
    path.chmod(0o600)


def _require_json(completed: subprocess.CompletedProcess[str], phase: str) -> object:
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{phase} returned non-JSON; inspect retained private streams"
        ) from exc


def installed_task_context_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Verify installed task context success, denial, privacy and SQLite immutability."""
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    parsed_redis = urlsplit(redis_url)
    if (
        parsed_redis.scheme != "redis"
        or parsed_redis.hostname != "127.0.0.1"
        or parsed_redis.port != 17386
        or parsed_redis.path != "/12"
        or parsed_redis.username is not None
        or parsed_redis.password is not None
        or parsed_redis.query
        or parsed_redis.fragment
    ):
        raise RuntimeError(
            "task context smoke requires the explicit isolated Redis DB 12 URL"
        )

    aig = aig.expanduser().absolute()
    python = python.expanduser().absolute()
    cwd = cwd.expanduser().absolute()
    workspace = cwd / f"installed-task-context-{uuid.uuid4().hex}"
    workspace.mkdir(mode=0o700)
    logs = workspace / "private-command-streams"
    logs.mkdir(mode=0o700)
    fixtures = workspace / "fixtures"
    fixtures.mkdir(mode=0o700)
    identity = workspace / ".aig" / "identity"
    identity.mkdir(parents=True, mode=0o700)
    database = workspace / ".aig" / "store.db"
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env.pop("PYTHONHOME", None)
    local_env["AIGINEERING_REDIS_URL"] = redis_url
    local_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")

    invocations: list[dict] = []

    def execute(
        phase: str,
        argv: list[str],
        *,
        expected_exit: int = 0,
        parse_json: bool = True,
        command_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], object | None]:
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace,
                env=command_env or local_env,
                text=True,
                capture_output=True,
                check=False,
                timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            number = len(invocations) + 1
            stdout_raw = logs / f"{number:04d}-{phase}-stdout.raw"
            stderr_raw = logs / f"{number:04d}-{phase}-stderr.raw"
            _write_private(stdout_raw, stdout)
            _write_private(stderr_raw, stderr)
            invocations.append(
                {
                    "phase": phase,
                    "argv": argv,
                    "exit_code": None,
                    "timed_out": True,
                    "stdout_sha256": _sha256(stdout.encode()),
                    "stderr_sha256": _sha256(stderr.encode()),
                }
            )
            _replace_manifest(logs / "commands.json", invocations)
            raise RuntimeError(
                f"{phase} timed out; private streams retained under {logs}"
            ) from None
        number = len(invocations) + 1
        stdout_raw = logs / f"{number:04d}-{phase}-stdout.raw"
        stderr_raw = logs / f"{number:04d}-{phase}-stderr.raw"
        _write_private(stdout_raw, completed.stdout)
        _write_private(stderr_raw, completed.stderr)
        invocations.append(
            {
                "phase": phase,
                "argv": argv,
                "exit_code": completed.returncode,
                "stdout_bytes": len(completed.stdout.encode("utf-8")),
                "stdout_sha256": _sha256(completed.stdout.encode("utf-8")),
                "stderr_bytes": len(completed.stderr.encode("utf-8")),
                "stderr_sha256": _sha256(completed.stderr.encode("utf-8")),
            }
        )
        _replace_manifest(logs / "commands.json", invocations)
        if completed.returncode != expected_exit:
            raise RuntimeError(
                f"{phase} exited {completed.returncode}; private streams retained under {logs}"
            )
        if not parse_json:
            return completed, None
        return completed, _require_json(completed, phase)

    def cli(
        phase: str,
        *args: str,
        expected_exit: int = 0,
        json_output: bool = True,
        command_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], object | None]:
        argv = [str(aig), "-C", str(workspace), *args]
        if json_output:
            argv.append("--json")
        return execute(
            phase,
            argv,
            expected_exit=expected_exit,
            parse_json=json_output,
            command_env=command_env,
        )

    def fingerprint(phase: str) -> dict:
        _completed, payload = execute(
            phase,
            [str(python), "-c", _DATABASE_FINGERPRINT, str(database)],
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("tables"), dict):
            raise RuntimeError(f"{phase} did not return the complete table fingerprint")
        return payload

    def read_only_cli(
        phase: str,
        args: tuple[str, ...],
        *,
        expected_exit: int = 0,
        json_output: bool = True,
        command_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], object | None]:
        before = fingerprint(f"{phase}-sqlite-before")
        completed, payload = cli(
            phase,
            *args,
            expected_exit=expected_exit,
            json_output=json_output,
            command_env=command_env,
        )
        after = fingerprint(f"{phase}-sqlite-after")
        if before != after:
            raise RuntimeError(f"{phase} changed one or more SQLite table fingerprints")
        _assert_private_stderr_absent(phase, completed.stderr)
        if json_output:
            _assert_success_envelope_revision(phase, payload, before.get("revision"))
        return completed, payload

    def write_content(filename: str, content: str) -> Path:
        path = fixtures / filename
        _write_private(path, content)
        return path

    def add_asset(
        name: str, filename: str, content: str, *, promptable: bool = True
    ) -> str:
        path = write_content(filename, content)
        args = ["asset", "add", "--name", name, "--content-file", str(path)]
        if not promptable:
            args.append("--no-promptable")
        _completed, result = cli(f"publish-{name}", *args)
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise RuntimeError(f"{name} signed Asset publication returned no exact ID")
        return result["id"]

    # Establish that both executables belong to one installed package, without
    # importing from the source checkout or starting a Worker.
    origin_code = (
        "import json,pathlib,sys,aigineering; "
        "print(json.dumps({'prefix':str(pathlib.Path(sys.prefix).absolute()),"
        "'module':str(pathlib.Path(aigineering.__file__).absolute())}))"
    )
    _origin_completed, origin = execute(
        "installed-python-origin", [str(python), "-c", origin_code]
    )
    if not isinstance(origin, dict):
        raise RuntimeError("installed Python origin probe was malformed")
    module = Path(origin.get("module", "")).resolve()
    prefix = Path(origin.get("prefix", "")).resolve()
    if (
        "/site-packages/aigineering/" not in str(module)
        or prefix != python.parent.parent.resolve()
        or aig.parent.resolve() != prefix / "bin"
    ):
        raise RuntimeError(
            "aig and Python do not resolve to one installed package environment"
        )

    _help_completed, _help = execute(
        "installed-cli-help", [str(aig), "--help"], parse_json=False
    )
    _domain_completed, domain = cli("domain-init", "domain", "init")
    if not isinstance(domain, dict) or not database.is_file():
        raise RuntimeError("isolated signed domain initialization failed")

    input_id = add_asset(
        "context-input",
        "context-input.txt",
        "Public fixture input " + _PRIVATE_MARKERS[0],
    )
    unrelated_id = add_asset(
        "context-unrelated",
        "context-unrelated.txt",
        "Unrelated fixture " + _PRIVATE_MARKERS[2],
    )
    hidden_id = add_asset(
        "context-hidden",
        "context-hidden.txt",
        "Hidden fixture " + _PRIVATE_MARKERS[3],
        promptable=False,
    )
    redacted_path = write_content(
        "context-redacted-candidate.txt",
        "Private redacted candidate " + _PRIVATE_MARKERS[1],
    )
    _redacted_completed, redacted_result = execute(
        "publish-redacted-candidate-asset",
        [
            str(python),
            "-c",
            _PUBLISH_REDACTED_ASSET,
            "context-redacted-view",
            str(redacted_path),
        ],
    )
    if not isinstance(redacted_result, dict) or not isinstance(
        redacted_result.get("id"), str
    ):
        raise RuntimeError("signed non-original Asset publication returned no exact ID")
    redacted_view_id = redacted_result["id"]

    _task_completed, task = cli(
        "publish-context-contract",
        "task",
        "create",
        "--name",
        "installed-task-context-fixture",
        "--description",
        _PRIVATE_MARKERS[4],
        "--input",
        "context-input",
        "--label",
        "context-redacted-view",
        "--output",
        "context-smoke-output",
        "--activation",
        "context-input",
        "--budget",
        "1",
    )
    if not isinstance(task, dict) or not isinstance(task.get("contract_id"), str):
        raise RuntimeError("signed context Contract publication returned no exact ID")
    contract_id = task["contract_id"]

    def success_envelope(phase: str, args: tuple[str, ...]) -> dict:
        _completed, payload = read_only_cli(phase, args)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(f"{phase} did not return the safe success envelope")
        data = payload.get("data")
        if (
            not isinstance(data, dict)
            or data.get("contract_id") != contract_id
            or not isinstance(data.get("disclosed_assets"), list)
            or not isinstance(data.get("method_context_assets"), list)
        ):
            raise RuntimeError(f"{phase} returned an unexpected task-context shape")
        expected_data_keys = {
            "contract_id",
            "disclosed_assets",
            "method_context_assets",
        }
        if "--asset" in args:
            expected_data_keys.add("selected_asset")
        if set(data) != expected_data_keys:
            raise RuntimeError(f"{phase} returned unexpected context data fields")
        if set(payload) != {"ok", "data", "revision"}:
            raise RuntimeError(f"{phase} returned unexpected top-level metadata")
        for row in (*data["disclosed_assets"], *data["method_context_assets"]):
            if (
                not isinstance(row, dict)
                or set(row) != _METADATA_KEYS
                or "content" in row
            ):
                raise RuntimeError(f"{phase} exposed non-allowlisted or body metadata")
        if any(
            marker in json.dumps(payload, ensure_ascii=False, sort_keys=True)
            for marker in _PRIVATE_MARKERS
        ):
            raise RuntimeError(f"{phase} exposed private description or Asset content")
        return payload

    metadata_result = success_envelope(
        "context-metadata-json", ("task", "context", contract_id)
    )
    data = metadata_result["data"]
    disclosed = data["disclosed_assets"]
    if [row.get("id") for row in disclosed] != [input_id, redacted_view_id]:
        raise RuntimeError(
            "disclosed metadata did not preserve exact input/context selection order"
        )
    if data["method_context_assets"] != []:
        raise RuntimeError("fixture unexpectedly acquired method continuation context")
    if any(row.get("id") in {unrelated_id, hidden_id} for row in disclosed):
        raise RuntimeError(
            "unrelated or nonpromptable Asset appeared in disclosed context"
        )
    redacted_metadata = next(row for row in disclosed if row["id"] == redacted_view_id)
    if (
        redacted_metadata.get("disclosure_view") != "redacted"
        or redacted_metadata.get("promptable") is not True
    ):
        raise RuntimeError(
            "redacted Asset metadata does not reflect its canonical signed representation"
        )

    metadata_selected = success_envelope(
        "context-exact-metadata-selection",
        ("task", "context", contract_id, "--asset", input_id),
    )["data"].get("selected_asset")
    if (
        not isinstance(metadata_selected, dict)
        or metadata_selected.get("id") != input_id
        or "content" in metadata_selected
    ):
        raise RuntimeError(
            "exact metadata selection returned content or the wrong Asset"
        )
    if set(metadata_selected) != _METADATA_KEYS:
        raise RuntimeError("selected metadata contains a field outside the allowlist")

    def exact_body_selection(
        payload: object, expected_id: str, expected_body: str
    ) -> None:
        if not isinstance(payload, dict) or set(payload) != {"ok", "data", "revision"}:
            raise RuntimeError("explicit body response has unexpected top-level fields")
        data = payload.get("data")
        if (
            payload.get("ok") is not True
            or not isinstance(data, dict)
            or set(data)
            != {
                "contract_id",
                "disclosed_assets",
                "method_context_assets",
                "selected_asset",
            }
            or data.get("contract_id") != contract_id
            or not isinstance(data.get("disclosed_assets"), list)
            or not isinstance(data.get("method_context_assets"), list)
        ):
            raise RuntimeError(
                "explicit body response has unexpected task context fields"
            )
        for row in (*data["disclosed_assets"], *data["method_context_assets"]):
            if (
                not isinstance(row, dict)
                or set(row) != _METADATA_KEYS
                or "content" in row
            ):
                raise RuntimeError(
                    "explicit body response exposed non-allowlisted list metadata"
                )
        selected = data.get("selected_asset")
        if (
            not isinstance(selected, dict)
            or set(selected) != _METADATA_KEYS | {"content"}
            or selected.get("id") != expected_id
            or selected.get("content") != expected_body
        ):
            raise RuntimeError(
                "explicit body response did not return only the exact selected Asset"
            )

    _original_completed, original_content = read_only_cli(
        "context-explicit-original-content",
        ("task", "context", contract_id, "--asset", input_id, "--include-content"),
    )
    exact_body_selection(
        original_content, input_id, "Public fixture input " + _PRIVATE_MARKERS[0]
    )
    selected_original = (
        original_content.get("data", {}).get("selected_asset")
        if isinstance(original_content, dict)
        else None
    )
    if (
        not isinstance(selected_original, dict)
        or selected_original.get("content")
        != "Public fixture input " + _PRIVATE_MARKERS[0]
    ):
        raise RuntimeError(
            "explicit exact original body did not return its selected content"
        )
    if any(
        marker in json.dumps(original_content, ensure_ascii=False)
        for marker in _PRIVATE_MARKERS[1:]
    ):
        raise RuntimeError(
            "explicit selected content exposed another Asset or task description"
        )

    _redacted_content_completed, redacted_content = read_only_cli(
        "context-explicit-redacted-content",
        (
            "task",
            "context",
            contract_id,
            "--asset",
            redacted_view_id,
            "--include-content",
        ),
    )
    exact_body_selection(redacted_content, redacted_view_id, _REDACTED)
    selected_redacted = (
        redacted_content.get("data", {}).get("selected_asset")
        if isinstance(redacted_content, dict)
        else None
    )
    if (
        not isinstance(selected_redacted, dict)
        or selected_redacted.get("content") != _REDACTED
    ):
        raise RuntimeError(
            "non-original selected Asset was not returned in its redacted representation"
        )
    if _PRIVATE_MARKERS[1] in json.dumps(redacted_content, ensure_ascii=False):
        raise RuntimeError("redacted content response exposed its stored private body")

    unknown_contract = "task:v6:" + "f" * 64

    def expect_error(
        phase: str,
        args: tuple[str, ...],
        code: str,
        *,
        json_output: bool = True,
        command_env: dict[str, str] | None = None,
        allow_redis_preflight_message: bool = False,
    ):
        completed, payload = read_only_cli(
            phase,
            args,
            expected_exit=1,
            json_output=json_output,
            command_env=command_env,
        )
        if json_output:
            _assert_error_envelope(
                phase,
                payload,
                code,
                allow_redis_preflight_message=allow_redis_preflight_message,
            )
            text = json.dumps(payload, ensure_ascii=False)
        else:
            text = completed.stdout + completed.stderr
            if f"({code})" not in text:
                raise RuntimeError(
                    f"{phase} human output omitted the stable error code"
                )
        if any(marker in text for marker in _PRIVATE_MARKERS):
            raise RuntimeError(
                f"{phase} denial leaked private content or hidden Asset details"
            )
        for identifier in (unrelated_id, hidden_id, unknown_contract):
            if identifier in text:
                raise RuntimeError(
                    f"{phase} denial echoed an undisclosed or unknown identifier"
                )
        return completed, payload

    expect_error(
        "context-undisclosed-unrelated-id",
        ("task", "context", contract_id, "--asset", unrelated_id),
        "asset_not_disclosed",
    )
    expect_error(
        "context-nonpromptable-hidden-id",
        ("task", "context", contract_id, "--asset", hidden_id, "--include-content"),
        "asset_not_disclosed",
    )
    expect_error(
        "context-unknown-task",
        ("task", "context", unknown_contract),
        "task_not_found",
    )
    expect_error(
        "context-invalid-content-flag",
        ("task", "context", contract_id, "--include-content"),
        "invalid_arguments",
    )
    expect_error(
        "context-human-unknown-task",
        ("task", "context", unknown_contract),
        "task_not_found",
        json_output=False,
    )
    outage_env = dict(local_env)
    outage_env["AIGINEERING_REDIS_URL"] = "redis://127.0.0.1:1/12"
    expect_error(
        "context-required-redis-outage",
        ("task", "context", contract_id),
        "redis_unavailable",
        command_env=outage_env,
        allow_redis_preflight_message=True,
    )

    # Human summaries expose the same metadata selection, but bodies only after
    # the explicit exact-ID opt-in.
    human_metadata, _ = read_only_cli(
        "context-human-metadata",
        ("task", "context", contract_id),
        json_output=False,
    )
    if not all(
        asset_id in human_metadata.stdout for asset_id in (input_id, redacted_view_id)
    ):
        raise RuntimeError("human metadata output omitted selected exact Asset IDs")
    human_rows = []
    for line in human_metadata.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                human_rows.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    if {row.get("id") for row in human_rows} != {input_id, redacted_view_id} or any(
        set(row) != _METADATA_KEYS for row in human_rows
    ):
        raise RuntimeError(
            "human metadata did not preserve the allowlisted exact Asset rows"
        )
    if any(
        marker in human_metadata.stdout + human_metadata.stderr
        for marker in _PRIVATE_MARKERS
    ):
        raise RuntimeError("human metadata output exposed private content")
    human_selected, _ = read_only_cli(
        "context-human-explicit-content",
        ("task", "context", contract_id, "--asset", input_id, "--include-content"),
        json_output=False,
    )
    if "Public fixture input " + _PRIVATE_MARKERS[0] not in human_selected.stdout:
        raise RuntimeError(
            "explicit human body selection omitted the exact selected content"
        )
    if any(marker in human_selected.stdout for marker in _PRIVATE_MARKERS[1:]):
        raise RuntimeError("human body selection exposed unrelated content")

    before_rebuild = fingerprint("sqlite-all-table-fingerprint-before-reopen-rebuild")
    _rebuild_completed, rebuild = execute(
        "sqlite-reopen-rebuild",
        [str(python), "-c", _REOPEN_REBUILD, str(database)],
    )
    if not isinstance(rebuild, dict) or not all(
        rebuild.get(key) is True
        for key in (
            "same_records_after_rebuild",
            "same_records_after_reopen",
            "same_records_after_second_rebuild",
            "stable_rebuild_digest",
        )
    ):
        raise RuntimeError(
            "Store reopen/rebuild changed immutable runtime record identities"
        )
    after_rebuild = fingerprint("sqlite-all-table-fingerprint-after-reopen-rebuild")
    if before_rebuild != after_rebuild:
        raise RuntimeError(
            "reopen/rebuild changed one or more of the full SQLite table fingerprints"
        )
    _after_rebuild_completed, after_rebuild_context = read_only_cli(
        "context-metadata-after-reopen-rebuild",
        ("task", "context", contract_id),
    )
    if after_rebuild_context != metadata_result:
        raise RuntimeError("task context changed after Store reopen/rebuild")

    commands = [row["argv"] for row in invocations]
    if any("run" in argv or "fleet" in argv for argv in commands):
        raise RuntimeError("fixture unexpectedly invoked a Worker or provider path")
    return {
        "phases": {
            "installed_executable_origin": "passed",
            "ordinary_signed_asset_and_contract_setup": "passed",
            "metadata_allowlist_and_exact_selection": "passed",
            "explicit_original_content_opt_in": "passed",
            "non_original_redaction": "passed",
            "unrelated_and_nonpromptable_denial": "passed",
            "unknown_task_and_invalid_arguments": "passed",
            "human_output_and_privacy": "passed",
            "required_redis_outage": "passed",
            "sqlite_read_only_fingerprints": "passed",
            "sqlite_reopen_rebuild": "passed",
            "tombstoned_asset_body_denial": "not_tested_no_ordinary_signed_tombstone_publisher",
            "method_context_assets": "not_tested_no_ordinary_method_continuation_fixture",
            "worker_or_provider_invocation": "not_run",
        },
        "contract_id": contract_id,
        "input_asset_id": input_id,
        "redacted_asset_id": redacted_view_id,
        "unrelated_asset_id": unrelated_id,
        "hidden_asset_id": hidden_id,
        "sqlite_revision": after_rebuild["revision"],
        "sqlite_table_count": len(after_rebuild["tables"]),
        "sqlite_tables": after_rebuild["tables"],
        "rebuild_record_count": rebuild["record_count"],
        "command_count": len(invocations),
        "private_evidence_dir": str(workspace),
        "private_command_stream_dir": str(logs),
    }
