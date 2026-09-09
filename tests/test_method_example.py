"""End-to-end coverage for the method governance example."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def test_method_governance_fixture_example(tmp_path):
    script = (
        Path(__file__).parents[1] / "examples" / "method-governance" / "demo.py"
    ).resolve()
    target = tmp_path / "method-run"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--worker",
            "fixture",
            "--directory",
            str(target),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "complete"
    assert output["rebuild_match"] is True
    assert output["method_id"].startswith("asset:")
    assert output["test_contract_id"].startswith("task:")
    assert output["evaluation_id"].startswith("asset:")

    overwrite = subprocess.run(
        [
            sys.executable,
            str(script),
            "--worker",
            "fixture",
            "--directory",
            str(target),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert overwrite.returncode != 0
    assert "refusing to overwrite" in overwrite.stderr

    missing_worker = subprocess.run(
        [sys.executable, str(script), "--directory", str(tmp_path / "no-worker")],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_worker.returncode != 0
    assert "--worker fixture" in missing_worker.stderr
