"""Explicit opt-in live direct Harness command; tests inject fake transport."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from types import MappingProxyType
import uuid
from typing import Any, Callable, Mapping

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from examples.thermal_calibration.deepseek_client import (  # noqa: E402
    DeepSeekChatCompletionClient,
    DeepSeekConfig,
    DirectLiveConfig,
    DirectLiveLimits,
    DirectLiveResult,
    ProviderAttemptEvent,
    SafeProviderError,
)
from examples.thermal_calibration.direct_harness import (  # noqa: E402
    DirectHarnessError,
    run_direct,
)
from examples.thermal_calibration.direct_fixture import (  # noqa: E402
    build_registry,
    fixture_case,
    fixture_standard,
)
from examples.thermal_calibration.provider_meter import RunBudget, RunMeter  # noqa: E402

_PACKAGE_ROOT = Path(__file__).resolve().parent
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_ENV_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EVENT_FIELDS = frozenset(
    {
        "kind",
        "event_type",
        "ordinal",
        "outcome",
        "status_code",
        "error_code",
        "error_type",
        "retryable",
        "duration_seconds",
        "finish_reason",
        "late",
        "remaining_timeout_seconds",
        "oracle_passed",
        "within_tolerance",
        "model_calls",
        "tool_attempts",
        "limits",
        "tool",
        "result_bytes",
    }
)


def main(
    argv: list[str] | None = None,
    *,
    environment_get: Callable[[str], str | None] | None = None,
    transport=None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(
        description="Run one explicitly configured live direct calibration request sequence"
    )
    parser.add_argument(
        "--case", choices=("near-threshold-fahrenheit", "short-interval"), required=True
    )
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--thinking-mode", choices=("enabled", "disabled"), required=True
    )
    parser.add_argument("--api-key-env", required=True)
    parser.add_argument("--request-timeout", type=float, required=True)
    parser.add_argument("--max-attempts", type=int, required=True)
    parser.add_argument("--max-output-tokens-per-request", type=int, required=True)
    parser.add_argument("--output-reservation-ceiling", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--max-model-calls", type=int, required=True)
    parser.add_argument("--max-tool-attempts", type=int, required=True)
    parser.add_argument("--max-response-bytes", type=int, required=True)
    parser.add_argument("--max-arguments-bytes", type=int, required=True)
    parser.add_argument("--max-final-bytes", type=int, required=True)
    parser.add_argument("--max-http-retries", type=int, required=True)
    parser.add_argument("--retry-backoff", type=float, required=True)
    args = parser.parse_args(argv)

    try:
        run_id = args.run_id or uuid.uuid4().hex
        provider = DeepSeekConfig(
            base_url=args.base_url,
            model=args.model,
            thinking_mode=args.thinking_mode,
        )
        limits = DirectLiveLimits(
            max_attempts=args.max_attempts,
            max_output_tokens_per_request=args.max_output_tokens_per_request,
            output_reservation_ceiling=args.output_reservation_ceiling,
            deadline_seconds=args.deadline,
            request_timeout_seconds=args.request_timeout,
            max_model_calls=args.max_model_calls,
            max_tool_attempts=args.max_tool_attempts,
            max_response_bytes=args.max_response_bytes,
            max_arguments_bytes=args.max_arguments_bytes,
            max_final_bytes=args.max_final_bytes,
            max_http_retries=args.max_http_retries,
            retry_backoff_seconds=args.retry_backoff,
        )
        config = DirectLiveConfig(
            case_id=args.case,
            output_dir=args.output_root,
            run_id=run_id,
            api_key_env=args.api_key_env,
            provider=provider,
            limits=limits,
        )
    except (TypeError, ValueError, OverflowError):
        _safe_stderr("configuration", None)
        return 2
    result = run_live(
        config,
        environment_get=environment_get,
        transport=transport,
        clock=clock,
        sleeper=sleeper,
    )
    return 0 if isinstance(result, DirectLiveResult) else result


def run_live(
    config: DirectLiveConfig,
    *,
    environment_get: Callable[[str], str | None] | None = None,
    transport=None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int | DirectLiveResult:
    """Run a configured live-mode path; fake transport is intended for tests."""
    try:
        output_root = _validate_config(config)
        case = fixture_case(config.case_id)
        standard = fixture_standard(config.case_id)
        brief_path = _PACKAGE_ROOT / "direct-brief.md"
        brief = brief_path.read_text(encoding="utf-8")
        case_path, standard_path = _fixture_paths(config.case_id)
        source_hashes = {
            "case_sha256": _sha(case_path),
            "standard_sha256": _sha(standard_path),
            "brief_sha256": _sha(brief_path),
            "tool_module_sha256": _sha(_PACKAGE_ROOT / "tools.py"),
            "oracle_module_sha256": _sha(_PACKAGE_ROOT / "oracle.py"),
            "direct_fixture_sha256": _sha(_PACKAGE_ROOT / "direct_fixture.py"),
            "direct_harness_sha256": _sha(_PACKAGE_ROOT / "direct_harness.py"),
            "deepseek_client_sha256": _sha(_PACKAGE_ROOT / "deepseek_client.py"),
            "direct_live_sha256": _sha(Path(__file__)),
        }
        run_dir = output_root / config.run_id
    except (TypeError, ValueError, OSError, KeyError, RuntimeError):
        _safe_stderr("configuration", None)
        return 2

    try:
        run_dir.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        _safe_stderr("configuration", None)
        return 2
    except OSError:
        _safe_stderr("configuration", None)
        return 2

    event_stream = None
    manifest_stream = None
    state = {
        "schema": "thermal-calibration-direct-live-v1",
        "run_id": config.run_id,
        "case_id": config.case_id,
        "status": "starting",
        "exit_code": None,
        "failure_code": None,
        "failure_type": None,
        "failure_phase": None,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": config.provider.model,
        "base_url": _redacted_base_url(config.provider.base_url),
        "thinking_mode": config.provider.thinking_mode,
        "limits": asdict(config.limits),
        "api_key_env": config.api_key_env,
        **source_hashes,
        "run_phase": "pre_request",
        "admitted_permits": 0,
        "transport_invocations": 0,
        "responses_observed": 0,
        "event_count": 0,
        "usage": None,
        "oracle_status": None,
        "provider_cost": None,
        "human_minutes": None,
        "manual_interventions": None,
        "total_cost": None,
        "total_cost_status": "incomplete",
    }
    try:
        event_stream = (run_dir / "events.jsonl").open("x", encoding="utf-8")
        manifest_stream = (run_dir / "manifest.json").open("x+", encoding="utf-8")
        _write_manifest(manifest_stream, state)
    except OSError:
        _safe_stderr("internal_error", run_dir)
        _close_quietly(event_stream)
        _close_quietly(manifest_stream)
        return 70

    sequence = 0

    def record(event):
        nonlocal sequence
        if isinstance(event, ProviderAttemptEvent):
            event_data = asdict(event)
            event_data["kind"] = "provider_attempt"
        elif isinstance(event, Mapping):
            event_data = dict(event)
        else:
            raise TypeError("event must be a safe event mapping")
        safe = {key: value for key, value in event_data.items() if key in _EVENT_FIELDS}
        sequence += 1
        safe_event = {
            "sequence": sequence,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            **safe,
        }
        event_stream.write(_json(safe_event) + "\n")
        event_stream.flush()
        os.fsync(event_stream.fileno())
        if safe.get("event_type") == "permit_admitted":
            state["admitted_permits"] += 1
        elif safe.get("event_type") == "transport_entered":
            state["transport_invocations"] += 1
        elif safe.get("event_type") == "response_observed":
            state["responses_observed"] += 1
        state["event_count"] = sequence

    environment_get = environment_get or os.environ.get
    try:
        state["run_phase"] = "key_lookup"
        secret = environment_get(config.api_key_env)
        if not isinstance(secret, str) or not secret:
            raise SafeProviderError(code="configuration", phase="configure")
        meter = RunMeter(
            RunBudget(
                max_attempts=config.limits.max_attempts,
                max_output_tokens_per_request=config.limits.max_output_tokens_per_request,
                output_reservation_ceiling=config.limits.output_reservation_ceiling,
                deadline_seconds=config.limits.deadline_seconds,
            ),
            clock=clock,
        )
        client = DeepSeekChatCompletionClient(
            config.provider,
            secret=secret,
            limits=config.limits,
            meter=meter,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            event_sink=record,
        )
        state["run_phase"] = "direct_tool_loop"
        loop_result = run_direct(
            client=client,
            case=case,
            standard=standard,
            brief=brief,
            registry=build_registry(),
            limits={
                "max_model_calls": config.limits.max_model_calls,
                "max_tool_attempts": config.limits.max_tool_attempts,
                "deadline_seconds": config.limits.deadline_seconds,
                "max_response_bytes": config.limits.max_response_bytes,
                "max_arguments_bytes": config.limits.max_arguments_bytes,
                "max_final_bytes": config.limits.max_final_bytes,
            },
            event_sink=record,
            monotonic=clock,
            response_protocol="deepseek-chat-v1",
        )
        state["oracle_status"] = {
            "passed": loop_result.oracle_result.get("passed") is True,
            "within_tolerance": loop_result.oracle_result.get("within_tolerance"),
        }
        state["usage"] = _usage_summary(meter.snapshot())
        if loop_result.oracle_result.get("passed") is not True:
            raise SafeProviderError(code="invalid_response", phase="validate")
        result = DirectLiveResult(
            report=_freeze_mapping(loop_result.report),
            oracle_result=_freeze_mapping(loop_result.oracle_result),
            usage=meter.snapshot(),
            event_count=sequence,
        )
        _exclusive_write(run_dir / "report.json", _json(_thaw(result.report)) + "\n")
        state["status"] = "completed"
        state["exit_code"] = 0
        state["run_phase"] = "complete"
        event_count = sequence
        state["event_count"] = event_count
        _write_manifest(manifest_stream, state)
        sys.stdout.write(
            _json(
                {
                    "ok": True,
                    "run_id": config.run_id,
                    "case_id": config.case_id,
                    "oracle_passed": True,
                    "within_tolerance": result.oracle_result.get("within_tolerance"),
                    "admitted_permits": state["admitted_permits"],
                    "transport_invocations": state["transport_invocations"],
                    "responses_observed": state["responses_observed"],
                    "event_count": event_count,
                    "output_dir": str(run_dir),
                }
            )
            + "\n"
        )
        return result
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, (SafeProviderError, DirectHarnessError))
            else "internal_error"
        )
        error_type = type(exc).__name__
        state["status"] = "failed"
        oversized_http_response = (
            code == "response_too_large"
            and isinstance(exc, SafeProviderError)
            and exc.status_code is not None
            and not 200 <= exc.status_code < 300
        )
        state["exit_code"] = (
            2
            if code == "configuration"
            else 3
            if code
            in {
                "request_budget_exhausted",
                "deadline_exceeded",
                "model_call_limit",
                "tool_attempt_limit",
            }
            else 4
            if code in {"transport_timeout", "transport_error", "client_timeout"}
            else 5
            if code in {"http_status", "retry_exhausted"} or oversized_http_response
            else 6
            if code
            in {
                "response_too_large",
                "invalid_json",
                "invalid_response",
                "unsupported_finish_reason",
                "protocol",
                "tool_arguments",
                "tool_input_schema",
                "registry_validation",
                "tool_output_limit",
                "final_output_limit",
            }
            else 70
        )
        state["failure_code"] = code
        state["failure_type"] = error_type
        state["failure_phase"] = state["run_phase"]
        state["run_phase"] = "failed"
        try:
            record(
                {
                    "kind": "run_failed",
                    "error_code": code,
                    "error_type": error_type,
                    "failure_phase": state["failure_phase"],
                }
            )
        except Exception:
            state["failure_code"] = "internal_error"
            state["exit_code"] = 70
            code = "internal_error"
        try:
            if "meter" in locals():
                state["usage"] = _usage_summary(meter.snapshot())
            _write_manifest(manifest_stream, state)
        except Exception:
            state["failure_code"] = "internal_error"
            state["exit_code"] = 70
            code = "internal_error"
        _safe_stderr(code, run_dir)
        return state["exit_code"]
    finally:
        _close_quietly(event_stream)
        _close_quietly(manifest_stream)


def _validate_config(config: DirectLiveConfig) -> Path:
    if not isinstance(config, DirectLiveConfig):
        raise TypeError("config must be DirectLiveConfig")
    if config.case_id not in {"near-threshold-fahrenheit", "short-interval"}:
        raise ValueError("unsupported case")
    if _RUN_ID_PATTERN.fullmatch(config.run_id) is None:
        raise ValueError("invalid run id")
    if _ENV_PATTERN.fullmatch(config.api_key_env) is None:
        raise ValueError("invalid environment variable name")
    if not isinstance(config.provider, DeepSeekConfig):
        raise ValueError("invalid provider configuration")
    DeepSeekConfig(
        base_url=config.provider.base_url,
        model=config.provider.model,
        thinking_mode=config.provider.thinking_mode,
    )
    if not isinstance(config.limits, DirectLiveLimits):
        raise ValueError("invalid limits")
    root_text = config.output_dir
    if (
        not isinstance(root_text, str)
        or not root_text
        or any(ord(ch) < 32 for ch in root_text)
    ):
        raise ValueError("invalid output root")
    root = Path(root_text).expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise ValueError("output root must already exist and be a directory")
    return root


def _fixture_paths(case_id: str) -> tuple[Path, Path]:
    manifest_path = _PACKAGE_ROOT / "fixtures" / "eval" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = [item for item in manifest["cases"] if item["case_id"] == case_id]
    if len(rows) != 1:
        raise ValueError("fixture manifest is invalid")
    root = manifest_path.parent
    return root / rows[0]["case_file"], root / rows[0]["standard_file"]


def _usage_summary(summary) -> dict[str, Any]:
    def component(value):
        return {
            "known_subtotal": value.known_subtotal,
            "unknown_count": value.unknown_count,
            "reported_count": value.reported_count,
            "value": value.value,
        }

    return {
        "admitted_attempts": summary.admitted_attempts,
        "output_reserved": summary.output_reserved,
        "pending_count": summary.pending_count,
        "prompt": component(summary.prompt),
        "completion": component(summary.completion),
        "total": component(summary.total),
        "inconsistent_total_attempts": summary.inconsistent_total_attempts,
        "cached_reasoning_status": summary.cached_reasoning_status,
        "attempts": [
            {
                "permit_id": attempt.permit_id,
                "outcome": attempt.outcome,
                "prompt_tokens": asdict(attempt.prompt_tokens),
                "completion_tokens": asdict(attempt.completion_tokens),
                "total_tokens": asdict(attempt.total_tokens),
                "late": attempt.late,
                "result_eligible": attempt.result_eligible,
                "inconsistent_total": attempt.inconsistent_total,
                "output_cap_violation": attempt.output_cap_violation,
                "cached_reasoning_status": attempt.cached_reasoning_status,
            }
            for attempt in summary.attempts
        ],
    }


def _freeze_mapping(value):
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_mapping(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_mapping(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_mapping(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _redacted_base_url(value: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    path = parsed.path
    return f"{parsed.scheme}://{parsed.hostname}{path}"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _write_manifest(handle, value: Mapping[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(_json(value) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _exclusive_write(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _safe_stderr(code: str, output_dir: Path | None) -> None:
    value = {"ok": False, "error_code": code}
    if output_dir is not None:
        value["output_dir"] = str(output_dir)
    sys.stderr.write(_json(value) + "\n")


def _close_quietly(handle) -> None:
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
