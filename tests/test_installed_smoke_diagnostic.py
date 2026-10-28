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


def _payload(**overrides):
    fields = {
        "run_id": "a" * 32,
        "phase": "input_validation",
        "error_code": "missing_input",
        "returncode": None,
        "source_revision": "b" * 40,
        "wheel_sha256": "c" * 64,
    }
    fields.update(overrides)
    return installed_smoke._safe_failure_payload(**fields)


def test_safe_failure_schema_is_closed_and_sanitizes_untrusted_values():
    assert installed_smoke._FAILURE_PHASES == {
        "input_validation",
        "venv_create",
        "wheel_install",
        "installed_import",
        "domain_init",
        "fixture_seed",
        "root_task_smoke",
        "feature_smokes",
        "reconstruction",
        "configured_run",
        "evidence_write",
        "post_rebuild_check",
        "unknown",
    }
    assert installed_smoke._FAILURE_CODES == {
        "missing_input",
        "command_failed",
        "invalid_response",
        "assertion_failed",
        "reconstruction_failed",
        "unexpected",
    }
    summary = _payload(
        phase="/private/workspace/private phase",
        error_code="SecretException: private detail",
        returncode=10**20,
        source_revision="TOKEN_GITHUB_SHA",
        wheel_sha256="private path",
    )

    assert set(summary) == {
        "schema",
        "run_id",
        "phase",
        "error_code",
        "returncode",
        "source_revision",
        "wheel_sha256",
    }
    assert summary == {
        "schema": "installed-smoke-failure/v1",
        "run_id": "a" * 32,
        "phase": "unknown",
        "error_code": "unexpected",
        "returncode": None,
        "source_revision": None,
        "wheel_sha256": None,
    }
    malformed = _payload(phase=[], error_code={"unexpected": True})
    assert malformed["phase"] == "unknown"
    assert malformed["error_code"] == "unexpected"
    assert (
        installed_smoke._failure_code(
            installed_smoke.SmokeFailure("ignored", error_code=[])
        )
        == "unexpected"
    )


def test_missing_wheel_is_captured_as_safe_nonzero_failure(monkeypatch, tmp_path):
    evidence = tmp_path / "upload"
    workdir = tmp_path / "missing-wheel-workspace"
    workdir.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "installed_smoke.py",
            "--wheel",
            str(tmp_path / "missing.whl"),
            "--expected-version",
            "0.5.dev0",
            "--source-root",
            str(tmp_path),
            "--evidence-dir",
            str(evidence),
        ],
    )

    try:
        installed_smoke.main()
    except RuntimeError as error:
        assert "phase=input_validation" in str(error)
        assert "error_code=missing_input" in str(error)
        assert str(tmp_path) not in str(error)
    else:
        raise AssertionError("missing wheel unexpectedly succeeded")

    summaries = list(evidence.glob("installed-smoke-failure-*.json"))
    assert len(summaries) == 1
    safe = json.loads(summaries[0].read_text())
    assert safe["phase"] == "input_validation"
    assert safe["error_code"] == "missing_input"
    assert safe["returncode"] is None
    private_files = list(workdir.glob("failed-installed-run-*.json"))
    assert len(private_files) == 1
    private = json.loads(private_files[0].read_text())
    assert "required wheel is missing" in private["error"]
    assert os.stat(private_files[0]).st_mode & 0o777 == 0o600


def test_invalid_wheel_resolution_is_captured_as_safe_failure(monkeypatch, tmp_path):
    evidence = tmp_path / "upload"
    workdir = tmp_path / "resolution-workspace"
    workdir.mkdir()
    wheel = tmp_path / "loop.whl"
    original_resolve = Path.resolve
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "installed_smoke.py",
            "--wheel",
            str(wheel),
            "--expected-version",
            "0.5.dev0",
            "--source-root",
            str(tmp_path),
            "--evidence-dir",
            str(evidence),
        ],
    )

    def fail_wheel_resolution(path, *args, **kwargs):
        if path == wheel:
            raise RuntimeError("private symlink loop marker")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_wheel_resolution)
    try:
        installed_smoke.main()
    except RuntimeError as error:
        assert "phase=input_validation" in str(error)
        assert "error_code=missing_input" in str(error)
        assert "private symlink loop marker" not in str(error)
    else:
        raise AssertionError("invalid wheel path unexpectedly succeeded")
    safe = json.loads(next(evidence.glob("installed-smoke-failure-*.json")).read_text())
    assert safe["error_code"] == "missing_input"
    assert safe["phase"] == "input_validation"
    private = json.loads(next(workdir.glob("failed-installed-run-*.json")).read_text())
    assert "private symlink loop marker" in private["traceback"]


def test_workspace_allocation_failure_still_emits_safe_failure(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(
        installed_smoke.tempfile,
        "mkdtemp",
        lambda **kwargs: (_ for _ in ()).throw(OSError("private allocation marker")),
    )
    try:
        with installed_smoke.smoke_workspace(
            evidence, failure_context={"run_id": "f" * 32}
        ):
            raise AssertionError("unreachable")
    except RuntimeError as error:
        assert "phase=venv_create" in str(error)
        assert "safe_capture=written" in str(error)
        assert "private allocation marker" not in str(error)
    else:
        raise AssertionError("workspace allocation failure was suppressed")
    summary = json.loads(
        next(evidence.glob("installed-smoke-failure-*.json")).read_text()
    )
    assert summary["error_code"] == "unexpected"
    assert summary["phase"] == "venv_create"


def test_raw_command_diagnostics_stay_private_and_failure_is_nonzero(
    monkeypatch, tmp_path
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    evidence = tmp_path / "evidence"
    marker = "PRIVATE_RUN_FAILURE_MARKER"
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    monkeypatch.setattr(
        installed_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=23, stdout=marker, stderr=marker
        ),
    )

    try:
        with installed_smoke.smoke_workspace(
            evidence,
            failure_context={
                "run_id": "d" * 32,
                "source_revision": None,
                "wheel_sha256": "e" * 64,
            },
        ):
            installed_smoke.run(["fixture"], cwd=tmp_path, env={})
    except RuntimeError as error:
        assert "error_code=command_failed" in str(error)
        assert "returncode=23" not in str(error)
        assert marker not in str(error)
        assert str(workdir) not in str(error)
    else:
        raise AssertionError("command failure was suppressed")

    safe = json.loads(
        (evidence / f"installed-smoke-failure-{'d' * 32}.json").read_text()
    )
    assert safe["error_code"] == "command_failed"
    assert safe["returncode"] == 23
    assert marker not in json.dumps(safe)
    private_files = list(workdir.glob("failed-installed-run-*.json"))
    assert len(private_files) == 1
    assert marker in json.loads(private_files[0].read_text())["error"]


def test_diagnostic_failure_retains_raw_private_record_without_overwriting_evidence(
    monkeypatch, tmp_path
):
    caller = tmp_path / "caller"
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    caller.mkdir()
    workspace.mkdir()
    evidence.mkdir()
    sentinel = evidence / "existing.json"
    sentinel.write_bytes(b"preserve\n")
    raw_out = "PRIVATE_DIAGNOSTIC_STDOUT"
    raw_err = "PRIVATE_DIAGNOSTIC_STDERR"
    monkeypatch.setattr(
        installed_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2,
            stdout=json.dumps({"status": "error", "error_type": "FileExistsError"})
            + raw_out,
            stderr=raw_err,
        ),
    )
    try:
        installed_smoke.diagnostic(
            Path("python"), Path("store.db"), evidence, cwd=workspace, env={}
        )
    except installed_smoke.SmokeFailure as error:
        assert error.error_code == "reconstruction_failed"
        assert error.returncode == 2
        assert raw_out not in str(error)
        assert raw_err not in str(error)
        assert str(workspace) not in str(error)
    else:
        raise AssertionError("failed diagnostic was not surfaced")

    private = next(workspace.glob("installed-diagnostic-failure-*.json"))
    record = json.loads(private.read_text())
    assert record["returncode"] == 2
    assert raw_out in record["stdout"]
    assert record["stderr"] == raw_err
    assert record["command"][-2:] == ["--output-dir", str(evidence)]
    assert os.stat(private).st_mode & 0o777 == 0o600
    assert sentinel.read_bytes() == b"preserve\n"
    assert list(caller.iterdir()) == []


def test_reconstruction_diagnostic_failure_is_safely_captured_end_to_end(
    monkeypatch, tmp_path
):
    workdir = tmp_path / "reconstruction-workspace"
    workdir.mkdir()
    evidence = tmp_path / "uploaded-evidence"
    run_id = "f" * 32
    raw_out = "PRIVATE_RECONSTRUCTION_STDOUT"
    raw_err = "PRIVATE_RECONSTRUCTION_STDERR"
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    monkeypatch.setattr(
        installed_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2,
            stdout=json.dumps({"error_type": "ReconstructionError", "detail": raw_out}),
            stderr=raw_err,
        ),
    )

    try:
        with installed_smoke.smoke_workspace(
            evidence,
            failure_context={"run_id": run_id, "source_revision": None},
        ) as actual_workdir:
            installed_smoke._set_phase("reconstruction")
            installed_smoke.diagnostic(
                Path("python"),
                actual_workdir / ".aig" / "store.db",
                evidence,
                cwd=actual_workdir,
                env={},
            )
    except RuntimeError as error:
        final_error = str(error)
    else:
        raise AssertionError("failed reconstruction diagnostic was suppressed")

    assert "phase=reconstruction" in final_error
    assert "error_code=reconstruction_failed" in final_error
    assert "private_capture=written" in final_error
    assert "safe_capture=written" in final_error
    assert raw_out not in final_error
    assert raw_err not in final_error
    assert str(workdir) not in final_error
    assert str(evidence) not in final_error

    uploaded_files = list(evidence.glob("installed-smoke-failure-*.json"))
    assert len(uploaded_files) == 1
    uploaded = json.loads(uploaded_files[0].read_text())
    assert uploaded["phase"] == "reconstruction"
    assert uploaded["error_code"] == "reconstruction_failed"
    assert uploaded["returncode"] == 2
    assert raw_out not in json.dumps(uploaded)
    assert raw_err not in json.dumps(uploaded)
    assert str(workdir) not in json.dumps(uploaded)
    assert str(evidence) not in json.dumps(uploaded)

    private_diagnostics = list(workdir.glob("installed-diagnostic-failure-*.json"))
    assert len(private_diagnostics) == 1
    private = json.loads(private_diagnostics[0].read_text())
    assert private["returncode"] == 2
    assert private["stdout"] == json.dumps(
        {"error_type": "ReconstructionError", "detail": raw_out}
    )
    assert private["stderr"] == raw_err
    assert str(workdir) in private["cwd"]
    assert str(evidence) in private["evidence_dir"]
    assert os.stat(private_diagnostics[0]).st_mode & 0o777 == 0o600

    private_runs = list(workdir.glob("failed-installed-run-*.json"))
    assert len(private_runs) == 1
    assert raw_out not in json.loads(private_runs[0].read_text())["error"]
    assert raw_err not in json.loads(private_runs[0].read_text())["error"]


def test_diagnostic_success_does_not_write_private_failure_record(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        installed_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout='{"status":"passed"}', stderr=""
        ),
    )
    assert installed_smoke.diagnostic(
        Path("python"), Path("store.db"), tmp_path / "evidence", cwd=workspace, env={}
    ) == {"status": "passed"}
    assert list(workspace.glob("installed-diagnostic-failure-*.json")) == []


def test_successful_workspace_still_cleans_up(monkeypatch, tmp_path):
    workspace = tmp_path / "successful-workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workspace)
    )
    with installed_smoke.smoke_workspace(None) as actual:
        assert actual == workspace
    assert not workspace.exists()


def test_failure_without_upload_directory_keeps_private_record(monkeypatch, tmp_path):
    workspace = tmp_path / "private-only-workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workspace)
    )
    marker = "PRIVATE_ONLY_FAILURE"
    try:
        with installed_smoke.smoke_workspace(None):
            raise OSError(marker)
    except RuntimeError as error:
        assert "safe_capture=not_configured" in str(error)
        assert marker not in str(error)
    else:
        raise AssertionError("private-only failure was suppressed")
    private = json.loads(
        next(workspace.glob("failed-installed-run-*.json")).read_text()
    )
    assert private["error"] == marker


def test_private_capture_failure_does_not_prevent_safe_capture(monkeypatch, tmp_path):
    workdir = tmp_path / "private-write-fails"
    workdir.mkdir()
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    monkeypatch.setattr(
        installed_smoke,
        "_write_private_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private")),
    )

    try:
        with installed_smoke.smoke_workspace(evidence):
            raise installed_smoke.SmokeFailure(
                "hidden", error_code="command_failed", returncode=7
            )
    except RuntimeError as error:
        assert "private_capture=failed" in str(error)
        assert "safe_capture=written" in str(error)
        assert "hidden" not in str(error)
    else:
        raise AssertionError("original failure was suppressed")
    safe = json.loads(next(evidence.glob("installed-smoke-failure-*.json")).read_text())
    assert safe["returncode"] == 7
    assert safe["error_code"] == "command_failed"


def test_safe_capture_failure_does_not_prevent_private_capture(monkeypatch, tmp_path):
    workdir = tmp_path / "safe-write-fails"
    workdir.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    monkeypatch.setattr(
        installed_smoke,
        "_write_safe_failure",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("safe")),
    )
    marker = "PRIVATE_CAPTURE_MARKER"

    try:
        with installed_smoke.smoke_workspace(tmp_path / "evidence"):
            raise ValueError(marker)
    except RuntimeError as error:
        assert "private_capture=written" in str(error)
        assert "safe_capture=failed" in str(error)
        assert marker not in str(error)
    else:
        raise AssertionError("original failure was suppressed")
    private = json.loads(next(workdir.glob("failed-installed-run-*.json")).read_text())
    assert marker in private["error"]


def test_both_capture_failures_preserve_original_nonzero_marker(monkeypatch, tmp_path):
    workdir = tmp_path / "both-captures-fail"
    workdir.mkdir()
    monkeypatch.setattr(
        installed_smoke.tempfile, "mkdtemp", lambda **kwargs: str(workdir)
    )
    monkeypatch.setattr(
        installed_smoke,
        "_write_private_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private marker")),
    )
    monkeypatch.setattr(
        installed_smoke,
        "_write_safe_failure",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("safe marker")),
    )
    try:
        with installed_smoke.smoke_workspace(tmp_path / "evidence"):
            raise installed_smoke.SmokeFailure(
                "raw failure", error_code="command_failed", returncode=19
            )
    except RuntimeError as error:
        assert "error_code=command_failed" in str(error)
        assert "private_capture=failed" in str(error)
        assert "safe_capture=failed" in str(error)
        assert "raw failure" not in str(error)
        assert "private marker" not in str(error)
        assert "safe marker" not in str(error)
    else:
        raise AssertionError("original failure was suppressed")


def test_capture_writes_are_unique_exclusive_and_leave_existing_files_unchanged(
    tmp_path,
):
    evidence = tmp_path / "evidence"
    first = _payload(run_id="1" * 32)
    second = _payload(run_id="2" * 32)
    first_path = installed_smoke._write_safe_failure(evidence, first)
    original = first_path.read_bytes()
    second_path = installed_smoke._write_safe_failure(evidence, second)
    assert first_path != second_path
    assert first_path.read_bytes() == original
    try:
        installed_smoke._write_safe_failure(evidence, first)
    except FileExistsError:
        pass
    else:
        raise AssertionError("exclusive safe capture overwrote an existing file")
    assert first_path.read_bytes() == original


def test_installed_smoke_workflow_has_required_redis_service_and_address():
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    installed_job = workflow.split("  installed-smoke:", 1)[1]
    assert "image: redis:7" in installed_job
    assert '--health-cmd "redis-cli ping"' in installed_job
    assert "AIGINEERING_REDIS_URL: redis://127.0.0.1:6379/14" in installed_job
    assert "if: always()" in installed_job
