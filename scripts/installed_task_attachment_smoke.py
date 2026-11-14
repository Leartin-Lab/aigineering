"""Installed CLI checks for read-only historical attachment inspection."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

_MARKER = "AIG_PRIVATE_ATTACHMENT_UNLINKED_20261114"
_FINGERPRINT = r"""
import hashlib,json,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]).resolve()
c=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True)
try:
 c.execute("BEGIN")
 names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
 tables={}
 for n in names:
  q='"'+n.replace('"','""')+'"'
  rows=c.execute('SELECT * FROM '+q).fetchall()
  norm=[[v.hex() if isinstance(v,bytes) else v for v in row] for row in rows]
  norm.sort(key=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'),default=str))
  raw=json.dumps(norm,sort_keys=True,separators=(',',':'),default=str).encode()
  tables[n]={'count':len(rows),'sha256':hashlib.sha256(raw).hexdigest()}
 revision=c.execute('SELECT COALESCE(MAX(revision),0) FROM runtime_records').fetchone()[0]
 digest=hashlib.sha256(json.dumps(tables,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 print(json.dumps({'revision':int(revision),'tables':tables,'sha256':digest},sort_keys=True))
finally:c.close()
"""


def _write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def installed_task_attachment_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Verify list is empty and export rejects an unlinked signed Asset read-only."""
    root = cwd / f"task-attachment-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    workspace, streams = root / "workspace", root / "streams"
    workspace.mkdir(mode=0o700)
    streams.mkdir(mode=0o700)
    database = workspace / ".aig" / "store.db"
    source = root / "ordinary-input.txt"
    _write(source, f"ordinary signed Asset fixture {_MARKER}\n".encode())
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env.pop("PYTHONHOME", None)
    commands: list[dict] = []

    def run(phase: str, argv: list[str], expected: int = 0, *, parse: bool = True):
        number = len(commands) + 1
        completed = subprocess.run(
            argv, cwd=workspace, env=local_env, capture_output=True, timeout=60
        )
        out_path = streams / f"{number:03}-{phase}.stdout.raw"
        err_path = streams / f"{number:03}-{phase}.stderr.raw"
        _write(out_path, completed.stdout)
        _write(err_path, completed.stderr)
        commands.append(
            {
                "phase": phase,
                "argv": argv,
                "exit": completed.returncode,
                "stdout": out_path.name,
                "stderr": err_path.name,
                "stdout_sha256": _sha(completed.stdout),
                "stderr_sha256": _sha(completed.stderr),
                "stdout_bytes": len(completed.stdout),
                "stderr_bytes": len(completed.stderr),
            }
        )
        if completed.returncode != expected:
            raise RuntimeError(
                f"{phase} exited {completed.returncode}; raw streams retained"
            )
        if _MARKER.encode() in completed.stdout + completed.stderr:
            raise RuntimeError(
                f"{phase} exposed private Asset content; raw streams retained"
            )
        if not parse:
            return completed, None
        try:
            payload = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"{phase} returned non-JSON; raw streams retained"
            ) from exc
        return completed, payload

    def cli(
        phase: str,
        *args: str,
        expected: int = 0,
        append_json: bool = True,
    ):
        argv = [str(aig), "-C", str(workspace), *args]
        if append_json:
            argv.append("--json")
        return run(phase, argv, expected)

    def fingerprint(phase: str) -> dict:
        _, value = run(phase, [str(python), "-c", _FINGERPRINT, str(database)])
        if not isinstance(value, dict) or not isinstance(value.get("tables"), dict):
            raise RuntimeError(f"{phase} returned an invalid Store fingerprint")
        return value

    try:
        _, origin = run(
            "installed-origin",
            [
                str(python),
                "-c",
                "import json,pathlib,sys,aigineering;print(json.dumps({'prefix':str(pathlib.Path(sys.prefix).resolve()),'module':str(pathlib.Path(aigineering.__file__).resolve())}))",
            ],
        )
        prefix = Path(origin["prefix"]).resolve()
        module = Path(origin["module"]).resolve()
        if (
            prefix != python.parent.parent.resolve()
            or aig.parent.resolve() != prefix / "bin"
            or not module.is_relative_to(prefix)
            or "site-packages" not in str(module)
        ):
            raise RuntimeError(
                "aig/Python are not bound to one source-external install"
            )

        cli("domain-init", "domain", "init")
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
        _, imported = cli(
            "asset-import",
            "artifact",
            "import",
            str(source),
            "--name",
            "attachment-unlinked",
            "--media-type",
            "text/plain",
            append_json=False,
        )
        asset_id = imported.get("id") if isinstance(imported, dict) else None
        if not isinstance(asset_id, str) or not asset_id:
            raise RuntimeError("ordinary signed Asset import returned no Asset ID")

        help_result, _ = run(
            "attachment-help", [str(aig), "task", "attachment", "--help"], parse=False
        )
        help_text = (help_result.stdout + help_result.stderr).decode(
            "utf-8", errors="replace"
        )
        if (
            not re.search(r"(?m)^\s+list\s", help_text)
            or not re.search(r"(?m)^\s+export\s", help_text)
            or re.search(r"(?m)^\s+add\s", help_text)
        ):
            raise RuntimeError("attachment help does not expose only list/export")

        before_list = fingerprint("list-before")
        _, listed = cli(
            "list-unlinked",
            "task",
            "attachment",
            "list",
            contract_id,
            "--store",
            str(database),
        )
        after_list = fingerprint("list-after")
        if before_list != after_list:
            raise RuntimeError("attachment list changed the Store fingerprint")
        if (
            not isinstance(listed, dict)
            or listed.get("ok") is not True
            or listed.get("data", {}).get("attachments") != []
            or listed.get("revision") != before_list.get("revision")
        ):
            raise RuntimeError(
                "read-only list did not return the empty exact attachment view"
            )

        export_path = root / "must-not-exist.bin"
        before_export = fingerprint("export-before")
        _, refused = cli(
            "export-unlinked",
            "task",
            "attachment",
            "export",
            contract_id,
            asset_id,
            "--output",
            str(export_path),
            "--store",
            str(database),
            expected=1,
        )
        after_export = fingerprint("export-after")
        if before_export != after_export:
            raise RuntimeError(
                "refused attachment export changed the Store fingerprint"
            )
        if (
            refused != {"ok": False, "error": "attachment_not_linked"}
            or export_path.exists()
        ):
            raise RuntimeError(
                "unlinked Asset export was not refused without creating a file"
            )

        _write(
            streams / "author-manifest.json",
            (
                json.dumps({"commands": commands}, sort_keys=True, indent=2) + "\n"
            ).encode(),
        )
        return {
            "status": "passed",
            "scope": "installed read-only list/export refusal for an unlinked signed Asset",
            "contract_id": contract_id,
            "asset_id": asset_id,
            "command_count": len(commands),
            "sqlite_table_count": len(before_export["tables"]),
            "list_store_fingerprint": before_list["sha256"],
            "export_store_fingerprint": before_export["sha256"],
            "retired_writer_assertions": [
                "attachment add",
                "add replay",
                "relay postcommit",
                "revocation replay",
                "state-neutral attachment publication",
            ],
            "source_historical_coverage": "tests/test_historical_control_records.py",
        }
    except Exception:
        _write(
            streams / "author-failure.json",
            (json.dumps({"commands": commands}, sort_keys=True) + "\n").encode(),
        )
        raise
