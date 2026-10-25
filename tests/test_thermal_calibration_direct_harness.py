"""Offline protocol tests for the direct calibration tool-call loop."""

from __future__ import annotations

import json
import ast
import copy
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from examples.thermal_calibration.direct_fixture import (
    DirectTransientToolError,
    ScriptedCalibrationClient,
    build_fault_registry,
    build_registry,
    fixture_case,
    fixture_standard,
)
from examples.thermal_calibration.direct_harness import (
    DirectHarnessError,
    _parse_object,
    run_direct,
)
from examples.thermal_calibration.oracle import evaluate_report
from tests.fixtures.thermal_calibration_direct_model import (
    FakeClock,
    RawResponseClient,
    ScriptedResponse,
)

SOURCE = Path(__file__).resolve().parents[1]
DIRECT_CLI = SOURCE / "examples/thermal_calibration/direct-run.py"


def _limits(**overrides):
    values = {
        "max_model_calls": 4,
        "max_tool_attempts": 3,
        "deadline_seconds": 30.0,
        "max_response_bytes": 1_048_576,
        "max_arguments_bytes": 524_288,
        "max_final_bytes": 262_144,
    }
    values.update(overrides)
    return values


def test_scripted_loop_uses_frozen_tools_and_external_oracle():
    case = fixture_case("near-threshold-fahrenheit")
    standard = fixture_standard("near-threshold-fahrenheit")
    client = ScriptedCalibrationClient(case, standard)
    events = []

    result = run_direct(
        client=client,
        case=case,
        standard=standard,
        brief="synthetic case; use both tools and return the exact report",
        registry=build_registry(),
        limits=_limits(),
        event_sink=events.append,
    )

    assert client.requested_tools == [
        "normalize_calibration",
        "analyze_calibration",
    ]
    first_tool_message = client.calls[1]["messages"][-1]
    normalized_wire_result = json.loads(first_tool_message["content"])["result"]
    assert client.requested_arguments[1]["normalized"] == json.loads(
        normalized_wire_result
    )
    assert result.report == json.loads(client.report_json)
    assert result.oracle_result == evaluate_report(case, standard, result.report)
    assert result.oracle_result["passed"] is True
    assert result.report["within_tolerance"] is False
    assert [event["kind"] for event in events].count("model_response") == 3
    assert [event["kind"] for event in events].count("tool_success") == 2
    assert events[-1]["kind"] == "run_completed"


def test_transient_fault_is_returned_to_model_and_failure_is_retained():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    client = ScriptedCalibrationClient(case, standard, retry_transient=True)
    events = []

    result = run_direct(
        client=client,
        case=case,
        standard=standard,
        brief="synthetic case",
        registry=build_fault_registry(fail_first_normalize=True),
        limits=_limits(max_model_calls=5, max_tool_attempts=3),
        event_sink=events.append,
    )

    assert result.oracle_result["passed"] is True
    assert client.calls[1]["messages"][-1]["role"] == "tool"
    assert (
        client.calls[1]["messages"][-1]["tool_call_id"] == client.requested_call_ids[0]
    )
    error_message = json.loads(client.calls[1]["messages"][-1]["content"])
    assert error_message == {
        "ok": False,
        "result": None,
        "error_code": "transient_tool_failure",
    }
    assert any(
        event["kind"] == "tool_transient_failure"
        and event["error_type"] == "DirectTransientToolError"
        for event in events
    )
    assert any(event["kind"] == "tool_success" for event in events)
    assert events[-1]["kind"] == "run_completed"


def test_exact_model_and_tool_limits_only_block_next_corresponding_call():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    client = ScriptedCalibrationClient(case, standard)

    result = run_direct(
        client=client,
        case=case,
        standard=standard,
        brief="synthetic case",
        registry=build_registry(),
        limits=_limits(max_model_calls=3, max_tool_attempts=2),
        event_sink=lambda _event: None,
    )

    assert result.oracle_result["passed"] is True
    assert len(client.calls) == 3

    client = ScriptedCalibrationClient(case, standard, request_extra_tool=True)
    events = []
    with pytest.raises(DirectHarnessError, match="tool attempt limit"):
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(max_model_calls=4, max_tool_attempts=2),
            event_sink=events.append,
        )
    assert events[-1]["kind"] == "run_failed"
    assert events[-1]["error_code"] == "tool_attempt_limit"
    assert len(client.calls) == 3


@pytest.mark.parametrize(
    "raw",
    [
        '{"message":{"role":"assistant","content":"{}","tool_calls":[]},"usage":null,"extra":1}',
        '{"message":{"role":"assistant","content":"{}","tool_calls":[]},"message":{},"usage":null}',
        '{"message":{"role":"assistant","content":"NaN","tool_calls":[]},"usage":null}',
        '{"message":{"role":"assistant","content":"","tool_calls":[]},"usage":null}',
        '{"message":{"role":"assistant","content":"{}","tool_calls":[],"extra":true},"usage":null}',
        '{"message":{"role":"assistant","content":null,"tool_calls":[{"id":"x","type":"function","function":{"name":"normalize_calibration","arguments":"{\\"case\\":{},\\"standard\\":{},\\"case\\":{}}"}}]},"usage":null}',
    ],
)
def test_malformed_wire_and_duplicate_json_keys_fail_closed(raw):
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    events = []
    with pytest.raises(DirectHarnessError):
        run_direct(
            client=RawResponseClient([raw]),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(),
            event_sink=events.append,
        )
    assert events[-1]["kind"] == "run_failed"


def test_offline_v1_still_rejects_non_null_usage():
    raw = '{"message":{"role":"assistant","content":"{}","tool_calls":[]},"usage":{"prompt_tokens":1}}'
    with pytest.raises(DirectHarnessError, match="unsupported shape or usage"):
        run_direct(
            client=RawResponseClient([raw]),
            case=fixture_case("short-interval"),
            standard=fixture_standard("short-interval"),
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(),
            event_sink=lambda _event: None,
        )


def test_repeated_tool_ids_and_parallel_calls_are_rejected_without_execution():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    registry = build_registry()
    events = []
    client = ScriptedCalibrationClient(case, standard, repeat_tool_id=True)

    with pytest.raises(DirectHarnessError, match="duplicate tool-call ID"):
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=registry,
            limits=_limits(max_model_calls=4),
            event_sink=events.append,
        )
    assert len([event for event in events if event["kind"] == "tool_success"]) == 1

    client = ScriptedCalibrationClient(case, standard, parallel_calls=True)
    events = []
    with pytest.raises(DirectHarnessError, match="exactly one tool call"):
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=registry,
            limits=_limits(),
            event_sink=events.append,
        )
    assert not [event for event in events if event["kind"] == "tool_success"]


@pytest.mark.parametrize(
    "limit",
    [0, -1, True, 2.0, "2", None],
)
def test_count_limits_require_positive_plain_integers(limit):
    with pytest.raises(ValueError, match="positive integer"):
        run_direct(
            client=ScriptedCalibrationClient(
                fixture_case("short-interval"),
                fixture_standard("short-interval"),
            ),
            case=fixture_case("short-interval"),
            standard=fixture_standard("short-interval"),
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(max_model_calls=limit),
            event_sink=lambda _event: None,
        )


@pytest.mark.parametrize(
    "field",
    [
        "max_model_calls",
        "max_tool_attempts",
        "max_response_bytes",
        "max_arguments_bytes",
        "max_final_bytes",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, 2.0, "2", None])
def test_every_count_and_byte_limit_requires_positive_plain_integer(field, value):
    limits = _limits()
    limits[field] = value
    with pytest.raises(ValueError, match="positive integer"):
        run_direct(
            client=RawResponseClient([]),
            case=fixture_case("short-interval"),
            standard=fixture_standard("short-interval"),
            brief="synthetic case",
            registry=build_registry(),
            limits=limits,
            event_sink=lambda _event: None,
        )


@pytest.mark.parametrize("deadline", [0, -1, True, float("inf"), float("nan"), "1"])
def test_deadline_requires_positive_finite_non_boolean_number(deadline):
    limits = _limits(deadline_seconds=deadline)
    with pytest.raises(ValueError, match="positive finite number"):
        run_direct(
            client=RawResponseClient([]),
            case=fixture_case("short-interval"),
            standard=fixture_standard("short-interval"),
            brief="synthetic case",
            registry=build_registry(),
            limits=limits,
            event_sink=lambda _event: None,
        )


def test_model_call_limit_blocks_next_call_but_not_the_response_at_limit():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    client = ScriptedCalibrationClient(case, standard)
    events = []
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(max_model_calls=2, max_tool_attempts=2),
            event_sink=events.append,
        )
    assert raised.value.code == "model_call_limit"
    assert len(client.calls) == 2
    assert len([event for event in events if event["kind"] == "tool_success"]) == 2
    assert events[-1]["error_code"] == "model_call_limit"


def test_response_arguments_and_final_byte_caps_fail_before_use():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    response = ScriptedResponse.tool_call("normalize_calibration", "{}")
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=RawResponseClient([response]),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(max_response_bytes=1),
            event_sink=lambda _event: None,
        )
    assert raised.value.code == "response_limit"

    response = ScriptedResponse.tool_call(
        "normalize_calibration", json.dumps({"case": case, "standard": standard})
    )
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=RawResponseClient([response]),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(max_arguments_bytes=8),
            event_sink=lambda _event: None,
        )
    assert raised.value.code == "tool_arguments"

    final = (
        '{"message":{"role":"assistant","content":"{}","tool_calls":[]},"usage":null}'
    )
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=RawResponseClient([final]),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(max_final_bytes=1),
            event_sink=lambda _event: None,
        )
    assert raised.value.code == "final_output_limit"


def test_client_timeout_and_unknown_client_error_are_observable():
    class FailingClient:
        def __init__(self, error):
            self.error = error

        def complete(self, *, messages, tools, timeout_seconds):
            del messages, tools, timeout_seconds
            raise self.error

    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    events = []
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=FailingClient(TimeoutError("private timeout")),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(),
            event_sink=events.append,
        )
    assert raised.value.code == "client_timeout"
    assert events[-1]["error_code"] == "client_timeout"

    events = []
    original = RuntimeError("do not serialize this")
    with pytest.raises(RuntimeError) as raised:
        run_direct(
            client=FailingClient(original),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(),
            event_sink=events.append,
        )
    assert raised.value is original
    assert events[-1]["error_code"] == "unclassified"
    assert "do not serialize this" not in json.dumps(events)


def test_fake_clock_deadline_is_cooperative_at_call_boundaries():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    clock = FakeClock()
    client = ScriptedCalibrationClient(case, standard, clock=clock, advance_on_call=0.6)
    events = []

    with pytest.raises(DirectHarnessError, match="deadline"):
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(deadline_seconds=0.5),
            event_sink=events.append,
            monotonic=clock.monotonic,
        )
    assert client.timeouts[0] == pytest.approx(0.5)
    assert len(client.calls) == 1
    assert events[-1]["kind"] == "run_failed"
    assert events[-1]["error_code"] == "deadline_exceeded"


def test_sync_handler_overrun_is_detected_after_return_not_claimed_as_preempted():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    clock = FakeClock()
    client = ScriptedCalibrationClient(case, standard, clock=clock)
    registry = build_registry()
    original = registry.run

    def slow_run(name, args):
        clock.advance(2.0)
        return original(name, args)

    registry.run = slow_run
    events = []
    with pytest.raises(DirectHarnessError, match="deadline"):
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=registry,
            limits=_limits(deadline_seconds=1.0),
            event_sink=events.append,
            monotonic=clock.monotonic,
        )
    assert len(client.calls) == 1
    assert events[-1]["kind"] == "run_failed"


def test_unknown_handler_exception_is_logged_then_propagated():
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    registry = build_registry()

    def fail(_args):
        raise RuntimeError("private detail must not enter event")

    spec = registry.get_spec("normalize_calibration")
    registry.register(spec, fail)
    client = ScriptedCalibrationClient(case, standard)
    events = []

    with pytest.raises(RuntimeError, match="private detail"):
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=registry,
            limits=_limits(),
            event_sink=events.append,
        )
    assert events[-1]["kind"] == "run_failed"
    assert events[-1]["error_type"] == "RuntimeError"
    assert "private detail" not in json.dumps(events)


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_overflowing_nested_argument_numbers_fail_before_registry_handler(number):
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    response = ScriptedResponse.tool_call(
        "normalize_calibration",
        f'{{"case":{{"nested":{{"value":{number}}}}},"standard":{{}}}}',
    )
    registry = build_registry()
    original_run = registry.run
    calls = []

    def counted_run(name, arguments):
        calls.append((name, arguments))
        return original_run(name, arguments)

    registry.run = counted_run
    events = []
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=RawResponseClient([response]),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=registry,
            limits=_limits(),
            event_sink=events.append,
        )
    assert raised.value.code == "tool_arguments"
    assert not calls
    assert events[-1]["error_code"] == "tool_arguments"
    assert _parse_object('{"finite":0.125}', phase="test") == {"finite": 0.125}


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_overflowing_final_report_numbers_are_protocol_failures(number):
    raw = _final_for_test(f'{{"drift_c_per_hour":{number}}}')
    events = []
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=RawResponseClient([raw]),
            case=fixture_case("short-interval"),
            standard=fixture_standard("short-interval"),
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(),
            event_sink=events.append,
        )
    assert raised.value.code == "protocol"
    assert events[-1]["error_code"] == "protocol"


def _final_for_test(report: str) -> str:
    return json.dumps(
        {
            "message": {"role": "assistant", "content": report, "tool_calls": []},
            "usage": None,
        },
        separators=(",", ":"),
    )


def test_client_mutation_does_not_change_history_or_future_tool_definitions():
    class MutatingClient:
        def __init__(self):
            self.delegate = ScriptedCalibrationClient(
                fixture_case("short-interval"), fixture_standard("short-interval")
            )
            self.received_messages = []
            self.received_tools = []

        def complete(self, *, messages, tools, timeout_seconds):
            self.received_messages.append(copy.deepcopy(messages))
            self.received_tools.append(copy.deepcopy(tools))
            for message in messages:
                if message.get("role") == "assistant" and message.get("tool_calls"):
                    message["tool_calls"][0]["function"]["arguments"] = "{}"
            tools[0]["function"]["name"] = "client_mutation"
            return self.delegate.complete(
                messages=messages, tools=tools, timeout_seconds=timeout_seconds
            )

    client = MutatingClient()
    result = run_direct(
        client=client,
        case=fixture_case("short-interval"),
        standard=fixture_standard("short-interval"),
        brief="synthetic case",
        registry=build_registry(),
        limits=_limits(),
        event_sink=lambda _event: None,
    )

    assert result.oracle_result["passed"] is True
    assert len(client.received_messages) == 3
    assert client.received_tools[0] == client.received_tools[1]
    assert client.received_tools[1] == client.received_tools[2]
    assistant_calls = [
        message
        for message in result.messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert [row["tool_calls"][0]["id"] for row in assistant_calls] == [
        "tool-1",
        "tool-2",
    ]
    assert json.loads(assistant_calls[0]["tool_calls"][0]["function"]["arguments"])[
        "case"
    ] == fixture_case("short-interval")


def _load_direct_cli_module():
    spec = importlib.util.spec_from_file_location(
        "thermal_calibration_direct_cli", DIRECT_CLI
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_cli_failure_manifest_records_protocol_code_type_and_phase(
    tmp_path, monkeypatch, capsys
):
    module = _load_direct_cli_module()

    class MalformedClient:
        def complete(self, *, messages, tools, timeout_seconds):
            del messages, tools, timeout_seconds
            return "not-json"

    monkeypatch.setattr(
        module, "ScriptedCalibrationClient", lambda *_args, **_kwargs: MalformedClient()
    )
    output_root = tmp_path / "runs"
    exit_code = module.main(
        [
            "--case",
            "short-interval",
            "--output-root",
            str(output_root),
            "--run-id",
            "protocol-failure",
        ]
    )
    capsys.readouterr()

    assert exit_code == 1
    manifest = json.loads(
        (output_root / "protocol-failure" / "manifest.json").read_text()
    )
    assert manifest["status"] == "failed"
    assert manifest["failure_code"] == "protocol"
    assert manifest["failure_type"] == "DirectHarnessError"
    assert manifest["failure_phase"] == "direct_tool_loop"
    assert manifest["exit_code"] == 1


def test_fixture_cli_setup_oserror_still_closes_and_writes_failure_manifest(tmp_path):
    copied_root = tmp_path / "copy"
    copied_package = copied_root / "examples" / "thermal_calibration"
    shutil.copytree(SOURCE / "examples/thermal_calibration", copied_package)
    shutil.copy2(SOURCE / "examples/__init__.py", copied_root / "examples/__init__.py")
    missing_case = copied_package / "fixtures/eval/near-threshold-fahrenheit.case.json"
    missing_case.unlink()
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(SOURCE), child_env.get("PYTHONPATH", "")) if item
    )
    module_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import examples.thermal_calibration.direct_fixture as module; "
            "print(module.__file__)",
        ],
        cwd=copied_root,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert module_probe.returncode == 0, module_probe.stderr
    assert (
        Path(module_probe.stdout.strip())
        .resolve()
        .is_relative_to(copied_package.resolve())
    )
    output_root = tmp_path / "runs"
    command = [
        sys.executable,
        str(copied_package / "direct-run.py"),
        "--case",
        "near-threshold-fahrenheit",
        "--output-root",
        str(output_root),
        "--run-id",
        "setup-failure",
    ]
    result = subprocess.run(
        command, cwd=copied_root, env=child_env, text=True, capture_output=True
    )

    assert result.returncode == 1
    run_dir = output_root / "setup-failure"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["failure_code"] == "unclassified"
    assert manifest["failure_type"] == "FileNotFoundError"
    assert manifest["failure_phase"] == "fixture_setup"
    assert manifest["case_sha256"] is None
    assert manifest["exit_code"] == 1
    assert (
        json.loads((run_dir / "stderr.txt").read_text())["error_type"]
        == "FileNotFoundError"
    )
    events_path = run_dir / "events.jsonl"
    event_rows = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert event_rows[-1]["kind"] == "run_failed"
    events_path.rename(run_dir / "events.closed.jsonl")


def test_fixture_cli_persists_success_and_transient_failure_without_overwrite(tmp_path):
    root = tmp_path / "runs"
    command = [
        sys.executable,
        str(DIRECT_CLI),
        "--run-id",
        "fixture-preserve-test",
        "--case",
        "short-interval",
        "--scenario",
        "transient",
        "--output-root",
        str(root),
    ]
    result = subprocess.run(command, cwd=SOURCE, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    run_dir = root / payload["run_id"]
    events = (run_dir / "events.jsonl").read_text().splitlines()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["usage"] is None
    assert manifest["usage_status"] == "unavailable"
    assert manifest["exit_code"] == 0
    assert manifest["total_cost"] is None
    assert manifest["total_cost_status"] == "incomplete"
    assert manifest["elapsed_seconds"] >= 0
    assert manifest["runner_sha256"]
    assert manifest["direct_harness_sha256"]
    assert manifest["direct_fixture_sha256"]
    event_rows = [json.loads(line) for line in events]
    assert [row["sequence"] for row in event_rows] == list(
        range(1, len(event_rows) + 1)
    )
    assert all(row["recorded_at_utc"] for row in event_rows)
    assert [json.loads(line)["kind"] for line in events].count(
        "tool_transient_failure"
    ) == 1
    assert (run_dir / "stdout.txt").exists()
    assert (run_dir / "stderr.txt").exists()

    collision = subprocess.run(command, cwd=SOURCE, text=True, capture_output=True)
    assert collision.returncode != 0
    assert json.loads((run_dir / "manifest.json").read_text()) == manifest


@pytest.mark.parametrize(
    ("tool_name", "arguments", "error_code"),
    [
        ("not_registered", "{}", "protocol"),
        ("normalize_calibration", "{}", "tool_input_schema"),
        (
            "normalize_calibration",
            '{"case":{},"case":{},"standard":{}}',
            "tool_arguments",
        ),
        ("normalize_calibration", '{"case":NaN,"standard":{}}', "tool_arguments"),
    ],
)
def test_unknown_tool_bad_schema_and_duplicate_or_nonfinite_arguments_fail(
    tool_name, arguments, error_code
):
    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    response = ScriptedResponse.tool_call(tool_name, arguments)
    events = []
    with pytest.raises(DirectHarnessError) as raised:
        run_direct(
            client=RawResponseClient([response]),
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(),
            event_sink=events.append,
        )
    assert raised.value.code == error_code
    assert events[-1]["kind"] == "run_failed"
    assert events[-1]["error_code"] == error_code


def test_oracle_rejection_is_distinct_from_loop_protocol_completion():
    raw = '{"message":{"role":"assistant","content":"{}","tool_calls":[]},"usage":null}'
    events = []
    result = run_direct(
        client=RawResponseClient([raw]),
        case=fixture_case("short-interval"),
        standard=fixture_standard("short-interval"),
        brief="synthetic case",
        registry=build_registry(),
        limits=_limits(),
        event_sink=events.append,
    )
    assert result.oracle_result["passed"] is False
    assert events[-1]["kind"] == "run_completed"
    assert events[-1]["oracle_passed"] is False


def test_direct_path_does_not_import_runtime_publication_or_scheduler():
    source = (SOURCE / "examples/thermal_calibration/direct_harness.py").read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "aigineering.agent.harness",
        "aigineering.agent.worker",
        "aigineering.core.commitment",
        "aigineering.runtime",
        "aigineering.local_fleet",
        "aigineering.core.sqlite_store",
    }
    assert not imported & forbidden
    assert "aigineering.core.tools" in imported
    assert "examples.thermal_calibration.oracle" in imported
    cli_source = (SOURCE / "examples/thermal_calibration/direct-run.py").read_text()
    assert "tests." not in cli_source
    assert "api_key" not in cli_source.lower()
    assert "http" not in cli_source.lower()


def test_transient_exception_type_is_explicit_and_narrow():
    assert DirectTransientToolError.__name__ == "DirectTransientToolError"


def test_provider_protocol_requires_explicit_opt_in_and_preserves_reasoning_turn():
    from examples.thermal_calibration.direct_harness import (
        DirectProviderResponse,
        ProviderAssistantMessage,
        ProviderToolCall,
    )

    case = fixture_case("short-interval")
    standard = fixture_standard("short-interval")
    registry = build_registry()
    normalized = json.loads(
        registry.run("normalize_calibration", {"case": case, "standard": standard})
    )
    report = json.loads(registry.run("analyze_calibration", {"normalized": normalized}))
    arguments = json.dumps({"case": case, "standard": standard}, separators=(",", ":"))
    first = DirectProviderResponse(
        message=ProviderAssistantMessage(
            role="assistant",
            content=None,
            tool_calls=(
                ProviderToolCall(
                    id="native-call-1",
                    name="normalize_calibration",
                    arguments=arguments,
                ),
            ),
            reasoning_content="keep this only in memory",
        ),
        finish_reason="tool_calls",
    )
    final = DirectProviderResponse(
        message=ProviderAssistantMessage(
            role="assistant",
            content=json.dumps(report, separators=(",", ":")),
            tool_calls=(),
            reasoning_content=None,
        ),
        finish_reason="stop",
    )

    class NativeClient:
        def __init__(self):
            self.requests = []
            self.responses = iter((first, final))

        def complete(self, *, messages, tools, timeout_seconds):
            self.requests.append(messages)
            return next(self.responses)

    client = NativeClient()
    with pytest.raises(DirectHarnessError, match="provider response"):
        run_direct(
            client=client,
            case=case,
            standard=standard,
            brief="synthetic case",
            registry=registry,
            limits=_limits(),
            event_sink=lambda _event: None,
        )

    client = NativeClient()
    result = run_direct(
        client=client,
        case=case,
        standard=standard,
        brief="synthetic case",
        registry=registry,
        limits=_limits(),
        event_sink=lambda _event: None,
        response_protocol="deepseek-chat-v1",
    )
    assert result.oracle_result["passed"] is True
    assert client.requests[1][-2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "native-call-1",
                "type": "function",
                "function": {
                    "name": "normalize_calibration",
                    "arguments": arguments,
                },
            }
        ],
        "reasoning_content": "keep this only in memory",
    }


def test_provider_finish_reason_mismatch_fails_before_tool_execution():
    from examples.thermal_calibration.direct_harness import (
        DirectProviderResponse,
        ProviderAssistantMessage,
        ProviderToolCall,
    )

    class NativeClient:
        def complete(self, *, messages, tools, timeout_seconds):
            return DirectProviderResponse(
                message=ProviderAssistantMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(
                        ProviderToolCall(
                            id="call-1",
                            name="normalize_calibration",
                            arguments="{}",
                        ),
                    ),
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )

    events = []
    with pytest.raises(DirectHarnessError, match="finish reason"):
        run_direct(
            client=NativeClient(),
            case=fixture_case("short-interval"),
            standard=fixture_standard("short-interval"),
            brief="synthetic case",
            registry=build_registry(),
            limits=_limits(),
            event_sink=events.append,
            response_protocol="deepseek-chat-v1",
        )
    assert not [event for event in events if event["kind"] == "tool_success"]
