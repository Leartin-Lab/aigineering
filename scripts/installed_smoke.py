"""Verify the installed wheel through independent CLI processes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def json_command(aig: Path, args: list[str], *, cwd: Path, env: dict[str, str]) -> dict:
    output = run([str(aig), *args, "--json"], cwd=cwd, env=env)
    return json.loads(output)


def diagnostic(
    python: Path, database: Path, evidence_dir: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    result = subprocess.run(
        [
            str(python),
            "-m",
            "aigineering.diagnostics",
            str(database),
            "--output-dir",
            str(evidence_dir),
        ],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"reconstruction diagnostic failed with exit code {result.returncode}"
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("reconstruction diagnostic returned invalid JSON") from error
    if report.get("status") != "passed":
        raise RuntimeError(
            f"reconstruction diagnostic did not pass: {report.get('status')}"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path)
    options = parser.parse_args()

    wheel = options.wheel.resolve()
    source_root = options.source_root.resolve()
    if not wheel.is_file():
        parser.error(f"wheel does not exist: {wheel}")

    with tempfile.TemporaryDirectory(prefix="aigineering-installed-smoke-") as raw:
        workdir = Path(raw)
        venv_dir = workdir / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = venv_dir / "bin" / "python"
        aig = venv_dir / "bin" / "aig"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AIG_", "AIGINEERING_", "PYTHONPATH", "PYTHONHOME"))
        }

        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            cwd=workdir,
            env=env,
        )
        module_path = run(
            [
                str(python),
                "-c",
                "import aigineering; print(aigineering.__file__); print(aigineering.__version__)",
            ],
            cwd=workdir,
            env=env,
        ).splitlines()
        if len(module_path) != 2 or Path(module_path[0]).resolve().is_relative_to(
            source_root
        ):
            raise RuntimeError(
                f"installed import resolved inside source tree: {module_path}"
            )
        if module_path[1] != options.expected_version:
            raise RuntimeError(
                f"installed version {module_path[1]!r} != {options.expected_version!r}"
            )

        domain = json_command(aig, ["domain", "init"], cwd=workdir, env=env)
        if not domain.get("domain_id"):
            raise RuntimeError("domain init did not return a domain ID")
        source = json_command(
            aig,
            ["asset", "add", "--name", "source", "--content", "smoke evidence"],
            cwd=workdir,
            env=env,
        )
        if not source.get("id"):
            raise RuntimeError("asset add did not return an asset ID")
        task = json_command(
            aig,
            [
                "task",
                "create",
                "--name",
                "installed_smoke",
                "--description",
                "installed wheel smoke",
                "--input",
                "source",
                "--activation",
                "source",
                "--output",
                "report",
            ],
            cwd=workdir,
            env=env,
        )
        contract_id = task.get("contract_id")
        if not contract_id:
            raise RuntimeError("task create did not return a contract ID")
        run_result = json_command(
            aig, ["run", "--once", "--worker", "mock"], cwd=workdir, env=env
        )
        if not run_result.get("ok"):
            raise RuntimeError(f"mock run did not complete: {run_result}")
        status = json_command(
            aig, ["task", "status", contract_id], cwd=workdir, env=env
        )
        if status.get("status") != "completed":
            raise RuntimeError(f"task did not complete: {status}")
        audit = json_command(aig, ["task", "audit", contract_id], cwd=workdir, env=env)
        if not isinstance(audit, dict):
            raise RuntimeError("task audit did not return an object")
        audit_task = audit.get("task")
        productivity = audit.get("productivity")
        if (
            not isinstance(audit_task, dict)
            or audit_task.get("contract_id") != contract_id
            or audit_task.get("status") != "completed"
            or not isinstance(productivity, dict)
            or productivity.get("root_contract_id") != contract_id
            or contract_id not in productivity.get("contract_ids", [])
        ):
            raise RuntimeError("task audit did not contain the completed root lineage")

        evidence_dir = (
            options.evidence_dir.resolve()
            if options.evidence_dir
            else workdir / "reconstruction-evidence"
        )
        report = diagnostic(
            python,
            workdir / ".aig" / "store.db",
            evidence_dir,
            cwd=workdir,
            env=env,
        )
        if (
            not (evidence_dir / "manifest.json").is_file()
            or report.get("status") != "passed"
        ):
            raise RuntimeError("reconstruction evidence is missing or incomplete")

        source_status = json_command(
            aig, ["task", "status", contract_id], cwd=workdir, env=env
        )
        if source_status.get("status") != "completed":
            raise RuntimeError(
                "source task status changed after reconstruction diagnostic"
            )
        reopen_check = run(
            [
                str(python),
                "-c",
                "from aigineering.core.sqlite_store import SQLiteStore; "
                f"s=SQLiteStore({str(workdir / '.aig' / 'store.db')!r}); "
                f"assert s.get_contract({contract_id!r}) is not None; s.close(); print('reopened')",
            ],
            cwd=workdir,
            env=env,
        )
        if reopen_check != "reopened":
            raise RuntimeError("SQLite reopen check did not complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
