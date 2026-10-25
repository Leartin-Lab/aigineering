"""No-network live CLI lifecycle tests using an explicitly fake transport."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

from examples.thermal_calibration.direct_fixture import (
    build_registry,
    fixture_case,
    fixture_standard,
)
from examples.thermal_calibration.deepseek_client import (
    DeepSeekConfig,
    DirectLiveConfig,
    DirectLiveLimits,
    DirectLiveResult,
    TransportReply,
)

SOURCE = Path(__file__).resolve().parents[1]
LIVE_CLI = SOURCE / "examples/thermal_calibration/direct-live.py"
_spec = importlib.util.spec_from_file_location(
    "thermal_calibration_direct_live", LIVE_CLI
)
assert _spec is not None and _spec.loader is not None
live = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = live
_spec.loader.exec_module(live)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _limits(**overrides):
    values = dict(
        max_attempts=4,
        max_output_tokens_per_request=64,
        output_reservation_ceiling=256,
        deadline_seconds=20.0,
        request_timeout_seconds=5.0,
        max_model_calls=4,
        max_tool_attempts=3,
        max_response_bytes=4096,
        max_arguments_bytes=4096,
        max_final_bytes=4096,
        max_http_retries=0,
        retry_backoff_seconds=0.0,
    )
    values.update(overrides)
    return DirectLiveLimits(**values)


def _config(root, **overrides):
    values = dict(
        case_id="short-interval",
        output_dir=str(root),
        run_id="run-live-001",
        api_key_env="TEST_DEEPSEEK_KEY",
        provider=DeepSeekConfig(
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            thinking_mode="disabled",
        ),
        limits=_limits(),
    )
    values.update(overrides)
    return DirectLiveConfig(**values)


def _native_body(message, finish_reason, *, usage=None, include_usage=True):
    value = {
        "id": "response-id",
        "object": "chat.completion",
        "created": 2,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": message,
                "logprobs": None,
            }
        ],
    }
    if include_usage:
        value["usage"] = usage
    return json.dumps(value, separators=(",", ":")).encode()


class QueueTransport:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.bodies.pop(0)
        if isinstance(reply, TransportReply):
            return reply
        return TransportReply(status=200, body=reply)


def _success_bodies():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    registry = build_registry()
    normalized = json.loads(
        registry.run("normalize_calibration", {"case": case, "standard": standard})
    )
    report = json.loads(registry.run("analyze_calibration", {"normalized": normalized}))
    first = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "normalize-1",
                "type": "function",
                "function": {
                    "name": "normalize_calibration",
                    "arguments": json.dumps(
                        {"case": case, "standard": standard}, separators=(",", ":")
                    ),
                },
            }
        ],
    }
    second = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "analyze-1",
                "type": "function",
                "function": {
                    "name": "analyze_calibration",
                    "arguments": json.dumps(
                        {"normalized": normalized}, separators=(",", ":")
                    ),
                },
            }
        ],
    }
    final = {
        "role": "assistant",
        "content": json.dumps(report, separators=(",", ":")),
        "tool_calls": [],
    }
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return [
        _native_body(first, "tool_calls", usage=usage),
        _native_body(second, "tool_calls", usage=usage),
        _native_body(final, "stop", usage=usage),
    ]


def _argv(root, **overrides):
    values = dict(
        case="short-interval",
        output_root=str(root),
        run_id="cli-run-001",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-flash",
        thinking_mode="disabled",
        api_key_env="TEST_DEEPSEEK_KEY",
        request_timeout="4",
        max_attempts="4",
        max_output_tokens_per_request="64",
        output_reservation_ceiling="256",
        deadline="20",
        max_model_calls="4",
        max_tool_attempts="3",
        max_response_bytes="4096",
        max_arguments_bytes="4096",
        max_final_bytes="4096",
        max_http_retries="0",
        retry_backoff="0",
    )
    values.update(overrides)
    args = []
    for name, value in values.items():
        args.extend(("--" + name.replace("_", "-"), value))
    return args


def test_live_cli_fake_transport_writes_safe_unique_run_evidence(tmp_path, capsys):
    transport = QueueTransport(_success_bodies())
    lookups = []
    code = live.main(
        _argv(tmp_path),
        environment_get=lambda name: lookups.append(name) or "dummy-test-key",
        transport=transport,
        clock=FakeClock(),
        sleeper=lambda _seconds: None,
    )
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert lookups == ["TEST_DEEPSEEK_KEY"]
    assert len(transport.calls) == 3
    assert "dummy-test-key" not in captured.out + captured.err
    run_dir = tmp_path / "cli-run-001"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["admitted_permits"] == 3
    assert manifest["transport_invocations"] == 3
    assert manifest["responses_observed"] == 3
    assert manifest["usage"]["prompt"]["known_subtotal"] == 30
    assert manifest["usage"]["prompt"]["unknown_count"] == 0
    assert manifest["provider_cost"] is None
    assert manifest["total_cost_status"] == "incomplete"
    assert json.loads((run_dir / "report.json").read_text())["schema"]
    all_evidence = "".join(
        path.read_text() for path in run_dir.iterdir() if path.is_file()
    )
    assert "dummy-test-key" not in all_evidence
    assert "messages" not in all_evidence


@pytest.mark.parametrize("status,expected_exit", [(200, 6), (500, 5), (503, 5)])
def test_cli_oversized_http_body_keeps_http_status_exit_class(
    status, expected_exit, tmp_path, capsys
):
    transport = QueueTransport([TransportReply(status=status, body=b"x" * 17)])
    code = live.main(
        _argv(
            tmp_path,
            run_id=f"oversized-{status}",
            max_response_bytes="16",
            max_http_retries="1",
        ),
        environment_get=lambda _name: "dummy-test-key",
        transport=transport,
        clock=FakeClock(),
    )
    capsys.readouterr()
    assert code == expected_exit
    assert len(transport.calls) == 1
    manifest = json.loads(
        (tmp_path / f"oversized-{status}" / "manifest.json").read_text()
    )
    assert manifest["failure_code"] == "response_too_large"
    assert manifest["exit_code"] == expected_exit
    assert manifest["admitted_permits"] == 1
    assert manifest["transport_invocations"] == 1
    assert manifest["responses_observed"] == 0
    assert manifest["usage"]["prompt"]["unknown_count"] == 1


def test_programmatic_success_returns_frozen_direct_result(tmp_path):
    result = live.run_live(
        _config(tmp_path, run_id="typed-result"),
        environment_get=lambda name: "dummy-test-key",
        transport=QueueTransport(_success_bodies()),
        clock=FakeClock(),
    )
    assert isinstance(result, DirectLiveResult)
    assert result.oracle_result["passed"] is True
    assert result.event_count > 0
    with pytest.raises(TypeError):
        result.report["injected"] = "no"


def test_live_evidence_omits_untrusted_call_id_and_reasoning_but_continues_exactly(
    tmp_path, capsys
):
    secret = "marker-dummy-secret"
    reasoning = "marker-private-reasoning"
    prompt_marker = "marker-private-prompt"
    call_id = "|".join((secret, reasoning, prompt_marker))
    bodies = _success_bodies()
    for index in (0, 1):
        value = json.loads(bodies[index])
        message = value["choices"][0]["message"]
        message["reasoning_content"] = reasoning
        if index == 0:
            message["tool_calls"][0]["id"] = call_id
        bodies[index] = json.dumps(value, separators=(",", ":")).encode()
    transport = QueueTransport(bodies)
    config = _config(tmp_path, run_id="marker-case")
    config = replace(
        config,
        provider=replace(config.provider, thinking_mode="enabled"),
    )
    result = live.run_live(
        config,
        environment_get=lambda _name: secret,
        transport=transport,
        clock=FakeClock(),
    )
    assert isinstance(result, DirectLiveResult)
    second_request = json.loads(transport.calls[1]["body"])
    assert any(
        message.get("tool_call_id") == call_id for message in second_request["messages"]
    )
    assert any(
        message.get("reasoning_content") == reasoning
        for message in second_request["messages"]
    )
    captured = capsys.readouterr()
    evidence = "\n".join(
        path.read_text()
        for path in (tmp_path / config.run_id).iterdir()
        if path.is_file()
    )
    for marker in (secret, reasoning, prompt_marker):
        assert marker not in captured.out + captured.err
        assert marker not in evidence


def test_invalid_configuration_and_environment_name_precede_directory_and_key(
    tmp_path, capsys
):
    lookups = []
    transport = QueueTransport([])
    bad_limit_args = _argv(tmp_path, max_attempts="-1")
    assert (
        live.main(
            bad_limit_args,
            environment_get=lambda name: lookups.append(name) or "dummy",
            transport=transport,
        )
        == 2
    )
    assert not lookups and not transport.calls
    assert not (tmp_path / "cli-run-001").exists()

    bad_env_args = _argv(tmp_path, api_key_env="NOT-AN-ENV-NAME")
    assert (
        live.main(
            bad_env_args,
            environment_get=lambda name: lookups.append(name) or "dummy",
            transport=transport,
        )
        == 2
    )
    assert not lookups and not transport.calls
    assert not (tmp_path / "cli-run-001").exists()
    assert (
        json.loads(capsys.readouterr().err.splitlines()[-1])["error_code"]
        == "configuration"
    )


def test_missing_key_writes_safe_failure_and_collision_does_not_lookup(
    tmp_path, capsys
):
    config = _config(tmp_path, run_id="missing-key")
    lookups = []
    assert (
        live.run_live(
            config,
            environment_get=lambda name: lookups.append(name) or "",
            transport=QueueTransport([]),
            clock=FakeClock(),
        )
        == 2
    )
    assert lookups == ["TEST_DEEPSEEK_KEY"]
    manifest = json.loads((tmp_path / "missing-key" / "manifest.json").read_text())
    assert manifest["failure_code"] == "configuration"
    assert manifest["status"] == "failed"

    existing = tmp_path / "collision"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("leave untouched")
    config = _config(tmp_path, run_id="collision")
    lookups.clear()
    assert (
        live.run_live(
            config,
            environment_get=lambda name: lookups.append(name) or "dummy",
            transport=QueueTransport([]),
            clock=FakeClock(),
        )
        == 2
    )
    assert not lookups
    assert marker.read_text() == "leave untouched"
    assert not (existing / "events.jsonl").exists()
    assert (
        json.loads(capsys.readouterr().err.splitlines()[-1])["error_code"]
        == "configuration"
    )


@pytest.mark.parametrize("failed_name", ["events.jsonl", "manifest.json"])
def test_initial_evidence_sink_failure_has_no_key_lookup_or_transport(
    tmp_path, monkeypatch, capsys, failed_name
):
    original_open = Path.open
    lookups = []
    transport = QueueTransport([])

    def failing_open(path, *args, **kwargs):
        if path.name == failed_name and path.parent.name.startswith("sink-failure-"):
            raise OSError("private filesystem failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    assert (
        live.run_live(
            _config(tmp_path, run_id=f"sink-failure-{Path(failed_name).stem}"),
            environment_get=lambda name: lookups.append(name) or "dummy",
            transport=transport,
            clock=FakeClock(),
        )
        == 70
    )
    assert not lookups and not transport.calls
    assert (tmp_path / f"sink-failure-{Path(failed_name).stem}").is_dir()
    assert (
        json.loads(capsys.readouterr().err.splitlines()[-1])["error_code"]
        == "internal_error"
    )


def test_invalid_output_root_has_no_directory_key_lookup_or_transport(tmp_path, capsys):
    missing_root = tmp_path / "does-not-exist"
    lookups = []
    transport = QueueTransport([])
    assert (
        live.main(
            _argv(missing_root),
            environment_get=lambda name: lookups.append(name) or "dummy",
            transport=transport,
        )
        == 2
    )
    assert not missing_root.exists()
    assert not lookups and not transport.calls
    assert (
        json.loads(capsys.readouterr().err.splitlines()[-1])["error_code"]
        == "configuration"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_attempts", True),
        ("max_attempts", -1),
        ("max_output_tokens_per_request", 393217),
        ("deadline_seconds", float("nan")),
        ("request_timeout_seconds", float("inf")),
        ("max_http_retries", 1.0),
        ("retry_backoff_seconds", -0.1),
        ("output_reservation_ceiling", 32),
    ],
)
def test_direct_limits_reject_invalid_types_and_boundaries(field, value):
    values = dict(
        max_attempts=1,
        max_output_tokens_per_request=64,
        output_reservation_ceiling=128,
        deadline_seconds=10.0,
        request_timeout_seconds=5.0,
        max_model_calls=1,
        max_tool_attempts=1,
        max_response_bytes=10,
        max_arguments_bytes=10,
        max_final_bytes=10,
        max_http_retries=0,
        retry_backoff_seconds=0.0,
    )
    values[field] = value
    with pytest.raises(ValueError):
        DirectLiveLimits(**values)


@pytest.mark.parametrize(
    "field",
    [
        "max_attempts",
        "max_output_tokens_per_request",
        "output_reservation_ceiling",
        "max_model_calls",
        "max_tool_attempts",
        "max_response_bytes",
        "max_arguments_bytes",
        "max_final_bytes",
        "max_http_retries",
    ],
)
@pytest.mark.parametrize("value", [True, -1, 2.0, "2", None])
def test_all_integer_limit_fields_reject_invalid_values(field, value):
    values = dict(
        max_attempts=1,
        max_output_tokens_per_request=64,
        output_reservation_ceiling=128,
        deadline_seconds=10.0,
        request_timeout_seconds=5.0,
        max_model_calls=1,
        max_tool_attempts=1,
        max_response_bytes=10,
        max_arguments_bytes=10,
        max_final_bytes=10,
        max_http_retries=0,
        retry_backoff_seconds=0.0,
    )
    values[field] = value
    with pytest.raises(ValueError):
        DirectLiveLimits(**values)


@pytest.mark.parametrize("field", ["deadline_seconds", "request_timeout_seconds"])
@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "2", None])
def test_all_positive_float_limits_reject_invalid_values(field, value):
    values = dict(
        max_attempts=1,
        max_output_tokens_per_request=64,
        output_reservation_ceiling=128,
        deadline_seconds=10.0,
        request_timeout_seconds=5.0,
        max_model_calls=1,
        max_tool_attempts=1,
        max_response_bytes=10,
        max_arguments_bytes=10,
        max_final_bytes=10,
        max_http_retries=0,
        retry_backoff_seconds=0.0,
    )
    values[field] = value
    with pytest.raises(ValueError):
        DirectLiveLimits(**values)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "0", None])
def test_backoff_rejects_invalid_values_but_zero_is_allowed(value):
    values = dict(
        max_attempts=1,
        max_output_tokens_per_request=64,
        output_reservation_ceiling=128,
        deadline_seconds=10.0,
        request_timeout_seconds=5.0,
        max_model_calls=1,
        max_tool_attempts=1,
        max_response_bytes=10,
        max_arguments_bytes=10,
        max_final_bytes=10,
        max_http_retries=0,
        retry_backoff_seconds=value,
    )
    with pytest.raises(ValueError):
        DirectLiveLimits(**values)


def test_zero_attempts_and_reservation_disable_requests_and_exact_caps_are_valid():
    limits = DirectLiveLimits(
        max_attempts=0,
        max_output_tokens_per_request=393_216,
        output_reservation_ceiling=0,
        deadline_seconds=0.001,
        request_timeout_seconds=0.001,
        max_model_calls=1,
        max_tool_attempts=1,
        max_response_bytes=1,
        max_arguments_bytes=1,
        max_final_bytes=1,
        max_http_retries=0,
        retry_backoff_seconds=0.0,
    )
    assert limits.max_attempts == 0
    assert limits.output_reservation_ceiling == 0
