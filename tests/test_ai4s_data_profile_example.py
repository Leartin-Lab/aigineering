"""Safety and replay contract for the scientific table profiling example."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "scientific-data-profile"
SCRIPT = EXAMPLE / "scripts" / "tabular_profile.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_profile_action_is_value_redacted_and_replayable():
    result = _run(
        "measurements.csv",
        "--root",
        str(EXAMPLE / "assets"),
        "--missing-token",
        "NA",
        "--action",
    )
    assert result.returncode == 0, result.stderr
    outer = json.loads(result.stdout.removeprefix("/exec "))
    profile = json.loads(outer["outputs"]["data_profile"])

    assert profile["schema_version"] == "scientific-tabular-profile-v1"
    assert profile["scanned_rows"] == 4
    assert profile["column_count"] == 4
    assert profile["columns"][2]["missing_count"] == 1
    assert profile["columns"][2]["inferred_type"] == "number"
    assert [column["field"] for column in profile["columns"]] == [
        "field_001",
        "field_002",
        "field_003",
        "field_004",
    ]
    assert "S001" not in result.stdout
    assert "treatment" not in result.stdout


def test_profile_reports_truncation_without_claiming_full_scan(tmp_path: Path):
    table = tmp_path / "data.csv"
    table.write_text("x,y\n1,a\n2,b\n3,c\n", encoding="utf-8")
    result = _run("data.csv", "--root", str(tmp_path), "--max-rows", "2")
    assert result.returncode == 0, result.stderr
    profile = json.loads(result.stdout)
    assert profile["scanned_rows"] == 2
    assert profile["truncated"] is True


def test_profile_fails_closed_on_malformed_rows(tmp_path: Path):
    (tmp_path / "bad.csv").write_text("x,y\n1\n", encoding="utf-8")
    result = _run("bad.csv", "--root", str(tmp_path))
    assert result.returncode == 2
    assert "expected 2" in result.stderr
    assert not result.stdout


def test_profile_rejects_paths_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "outside-profile.csv"
    outside.write_text("x\n1\n", encoding="utf-8")
    try:
        result = _run("../outside-profile.csv", "--root", str(tmp_path))
        assert result.returncode == 2
        assert "traversal" in result.stderr
    finally:
        outside.unlink()


def test_profile_rejects_symlink_when_platform_supports_it(tmp_path: Path):
    target = tmp_path / "target.csv"
    target.write_text("x\n1\n", encoding="utf-8")
    link = tmp_path / "link.csv"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    result = _run("link.csv", "--root", str(tmp_path))
    assert result.returncode == 2
    assert "symbolic links" in result.stderr
