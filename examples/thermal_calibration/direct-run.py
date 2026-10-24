"""Run only the deterministic offline direct-loop fixture."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from examples.thermal_calibration.direct_fixture import (  # noqa: E402
    ScriptedCalibrationClient,
    build_fault_registry,
    build_registry,
    fixture_case,
    fixture_standard,
)
from examples.thermal_calibration.direct_harness import (  # noqa: E402
    run_direct,
)

_PACKAGE_ROOT = Path(__file__).resolve().parent
_LIMITS = {
    "max_model_calls": 5,
    "max_tool_attempts": 3,
    "deadline_seconds": 30.0,
    "max_response_bytes": 1_048_576,
    "max_arguments_bytes": 524_288,
    "max_final_bytes": 262_144,
}
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the offline direct calibration fixture"
    )
    parser.add_argument(
        "--case",
        choices=("near-threshold-fahrenheit", "short-interval"),
        default="near-threshold-fahrenheit",
    )
    parser.add_argument("--scenario", choices=("normal", "transient"), default="normal")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    run_id = args.run_id or uuid.uuid4().hex
    if _RUN_ID.fullmatch(run_id) is None:
        parser.error("--run-id must be a safe nonempty identifier")
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_id
    try:
        run_dir.mkdir(exist_ok=False)
    except FileExistsError:
        print(json.dumps({"ok": False, "error": "run_id_exists"}), file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc).isoformat()
    started_monotonic = time.monotonic()
    event_sequence = 0
    event_stream = None
    stdout_text = ""
    stderr_text = ""
    status = "failed"
    oracle_passed = None
    failure_code = None
    failure_type = None
    failure_phase = None
    phase = "fixture_setup"
    case_path = None
    standard_path = None
    brief_path = _PACKAGE_ROOT / "direct-brief.md"
    result = None

    def record(event):
        nonlocal event_sequence
        if event_stream is None:
            return
        event_sequence += 1
        event = {
            "sequence": event_sequence,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        event_stream.write(_json(event) + "\n")
        event_stream.flush()
        os.fsync(event_stream.fileno())

    try:
        event_stream = (run_dir / "events.jsonl").open("x", encoding="utf-8")
        case = fixture_case(args.case)
        standard = fixture_standard(args.case)
        case_path, standard_path = _fixture_paths(args.case)
        brief = brief_path.read_text(encoding="utf-8")
        registry = (
            build_fault_registry(fail_first_normalize=True)
            if args.scenario == "transient"
            else build_registry()
        )
        client = ScriptedCalibrationClient(
            case,
            standard,
            retry_transient=args.scenario == "transient",
        )
        phase = "direct_tool_loop"
        result = run_direct(
            client=client,
            case=case,
            standard=standard,
            brief=brief,
            registry=registry,
            limits=_LIMITS,
            event_sink=record,
        )
        oracle_passed = result.oracle_result.get("passed") is True
        status = "completed" if oracle_passed else "oracle_rejected"
        if not oracle_passed:
            failure_code = "oracle_rejected"
            failure_type = "OracleRejection"
            failure_phase = "oracle"
        stdout_text = (
            _json(
                {
                    "ok": oracle_passed,
                    "run_id": run_id,
                    "case_id": args.case,
                    "oracle": result.oracle_result,
                    "report": result.report,
                }
            )
            + "\n"
        )
        if not oracle_passed:
            stderr_text = "oracle rejected the final report\n"
    except Exception as exc:
        code = getattr(exc, "code", "unclassified")
        failure_code = code if isinstance(code, str) and code else "unclassified"
        failure_type = type(exc).__name__
        failure_phase = phase
        status = "failed"
        stderr_text = (
            _json(
                {
                    "ok": False,
                    "run_id": run_id,
                    "error_code": failure_code,
                    "error_type": failure_type,
                    "failure_phase": failure_phase,
                }
            )
            + "\n"
        )
        record(
            {
                "kind": "run_failed",
                "error_code": failure_code,
                "error_type": failure_type,
                "failure_phase": failure_phase,
            }
        )
    finally:
        if event_stream is not None:
            try:
                event_stream.close()
            except OSError as exc:
                if failure_code is None:
                    failure_code = "event_log_close_failed"
                    failure_type = type(exc).__name__
                    failure_phase = "evidence_finalize"
                    status = "failed"
                    oracle_passed = False
        _exclusive_write(run_dir / "stdout.txt", stdout_text)
        _exclusive_write(run_dir / "stderr.txt", stderr_text)
        if status == "completed" and oracle_passed:
            assert result is not None
            _exclusive_write(run_dir / "report.json", _json(result.report) + "\n")
        manifest = {
            "schema": "thermal-calibration-direct-run-v1",
            "run_id": run_id,
            "started_at_utc": started_at,
            "elapsed_seconds": max(0.0, time.monotonic() - started_monotonic),
            "status": status,
            "exit_code": 0 if status == "completed" else 1,
            "failure_code": failure_code,
            "failure_type": failure_type,
            "failure_phase": failure_phase,
            "scenario": args.scenario,
            "case_id": args.case,
            "case_sha256": _sha_or_none(case_path),
            "standard_sha256": _sha_or_none(standard_path),
            "brief_sha256": _sha_or_none(brief_path),
            "tool_module_sha256": _sha_or_none(_PACKAGE_ROOT / "tools.py"),
            "oracle_module_sha256": _sha_or_none(_PACKAGE_ROOT / "oracle.py"),
            "runner_sha256": _sha_or_none(Path(__file__)),
            "direct_harness_sha256": _sha_or_none(_PACKAGE_ROOT / "direct_harness.py"),
            "direct_fixture_sha256": _sha_or_none(_PACKAGE_ROOT / "direct_fixture.py"),
            "model": "scripted",
            "limits": _LIMITS,
            "oracle_passed": oracle_passed,
            "usage": None,
            "usage_status": "unavailable",
            "provider_cost": None,
            "human_minutes": None,
            "manual_interventions": None,
            "total_cost": None,
            "total_cost_status": "incomplete",
        }
        _exclusive_write(run_dir / "manifest.json", _json(manifest) + "\n")

    if stdout_text:
        sys.stdout.write(stdout_text)
    if stderr_text:
        sys.stderr.write(stderr_text)
    return 0 if status == "completed" else 1


def _fixture_paths(case_id: str) -> tuple[Path, Path]:
    manifest_path = _PACKAGE_ROOT / "fixtures" / "eval" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = [item for item in manifest["cases"] if item["case_id"] == case_id]
    if len(rows) != 1:
        raise ValueError("fixture manifest is invalid")
    fixture_root = manifest_path.parent
    return fixture_root / rows[0]["case_file"], fixture_root / rows[0]["standard_file"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_or_none(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return _sha(path)
    except OSError:
        return None


def _json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _exclusive_write(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


if __name__ == "__main__":
    raise SystemExit(main())
