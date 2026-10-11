from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "installed_smoke.py"
SPEC = importlib.util.spec_from_file_location("installed_smoke_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
installed_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installed_smoke)


def test_diagnostic_failure_saves_private_raw_output_outside_evidence_dir(
    monkeypatch, tmp_path: Path
):
    caller_dir = tmp_path / "caller"
    instance_dir = tmp_path / "instance"
    caller_dir.mkdir()
    instance_dir.mkdir()
    monkeypatch.chdir(caller_dir)
    evidence_dir = tmp_path / "shared-evidence"
    evidence_dir.mkdir()
    sentinel = evidence_dir / "other-instance.json"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        installed_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2,
            stdout='{"status":"error","error_type":"FileExistsError"}',
            stderr="private diagnostic details",
        ),
    )

    try:
        installed_smoke.diagnostic(
            Path("python"), Path("store.db"), evidence_dir, cwd=instance_dir, env={}
        )
    except RuntimeError as error:
        assert "exit=2" in str(error)
        assert "error_type=FileExistsError" in str(error)
        failure_path = Path(str(error).split("private details saved to ", 1)[1])
    else:
        raise AssertionError("diagnostic failure was not surfaced")

    record = json.loads(failure_path.read_text(encoding="utf-8"))
    assert record["returncode"] == 2
    assert record["error_type"] == "FileExistsError"
    assert record["stdout"] == '{"status":"error","error_type":"FileExistsError"}'
    assert record["stderr"] == "private diagnostic details"
    assert record["command"][-2:] == ["--output-dir", str(evidence_dir)]
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert failure_path.parent == instance_dir
    assert list(caller_dir.glob("installed-diagnostic-failure-*.json")) == []
    assert os.stat(failure_path).st_mode & 0o777 == 0o600


def test_diagnostic_success_does_not_write_failure_record(monkeypatch, tmp_path: Path):
    caller_dir = tmp_path / "caller"
    instance_dir = tmp_path / "instance"
    caller_dir.mkdir()
    instance_dir.mkdir()
    monkeypatch.chdir(caller_dir)
    monkeypatch.setattr(
        installed_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout='{"status":"passed"}', stderr=""
        ),
    )

    assert installed_smoke.diagnostic(
        Path("python"),
        Path("store.db"),
        tmp_path / "evidence",
        cwd=instance_dir,
        env={},
    ) == {"status": "passed"}
    assert list(caller_dir.glob("installed-diagnostic-failure-*.json")) == []
    assert list(instance_dir.glob("installed-diagnostic-failure-*.json")) == []


def test_smoke_workspace_failure_summary_stays_private_and_preserves_evidence(
    monkeypatch, tmp_path: Path
):
    workdir = tmp_path / "instance-workspace"
    workdir.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    evidence_dir = tmp_path / "shared-evidence"
    evidence_dir.mkdir()
    preexisting = evidence_dir / "failed-installed-run.json"
    preexisting.write_bytes(b"other instance evidence\n")
    initial_listing = sorted(path.name for path in evidence_dir.iterdir())

    try:
        with installed_smoke.smoke_workspace(evidence_dir) as actual_workdir:
            assert actual_workdir == workdir
            raise ValueError("private traceback detail")
    except RuntimeError as error:
        assert "error_type=ValueError" in str(error)
        assert "private traceback detail" not in str(error)
        assert f"workspace preserved at {workdir}" in str(error)
        summary_path = Path(str(error).split("private summary: ", 1)[1])
    else:
        raise AssertionError("smoke workspace failure was not surfaced")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    private_traceback = summary.pop("traceback")
    assert "Traceback (most recent call last):" in private_traceback
    assert "ValueError: private traceback detail" in private_traceback
    assert "smoke_workspace" in private_traceback
    assert summary == {
        "workspace": str(workdir),
        "evidence_dir": str(evidence_dir),
        "error_type": "ValueError",
        "error": "private traceback detail",
    }
    assert summary_path.parent == workdir
    assert os.stat(summary_path).st_mode & 0o777 == 0o600
    assert preexisting.read_bytes() == b"other instance evidence\n"
    assert sorted(path.name for path in evidence_dir.iterdir()) == initial_listing


def test_smoke_workspace_success_still_cleans_workspace(monkeypatch, tmp_path: Path):
    workdir = tmp_path / "successful-workspace"
    workdir.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )

    with installed_smoke.smoke_workspace(None) as actual_workdir:
        assert actual_workdir == workdir

    assert not workdir.exists()


def test_smoke_workspace_without_evidence_dir_still_retains_failure(
    monkeypatch, tmp_path: Path
):
    workdir = tmp_path / "no-evidence-workspace"
    workdir.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )

    try:
        with installed_smoke.smoke_workspace(None):
            raise OSError("private setup detail")
    except RuntimeError as error:
        summary_path = Path(str(error).split("private summary: ", 1)[1])
    else:
        raise AssertionError("smoke workspace failure was not surfaced")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["evidence_dir"] is None
    assert summary["error_type"] == "OSError"
    assert summary["error"] == "private setup detail"
    assert summary_path.parent == workdir
    assert os.stat(summary_path).st_mode & 0o777 == 0o600
