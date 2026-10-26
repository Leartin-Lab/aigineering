from __future__ import annotations

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from aigineering.agent.worker import WorkerExecutionError
from examples.thermal_calibration import runtime_meter
from examples.thermal_calibration.provider_meter import RunMeter


def _env(monkeypatch, *, attempts="3", ceiling="24576", deadline="120"):
    monkeypatch.setenv("AIGINEERING_G0C_MAX_ATTEMPTS", attempts)
    monkeypatch.setenv("AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING", ceiling)
    monkeypatch.setenv("AIGINEERING_G0C_RUN_DEADLINE_SECONDS", deadline)
    monkeypatch.setenv("AIGINEERING_API_KEY", "dummy-g0c-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime_meter.reset_process_session_for_tests()


def _planner_payload():
    return {
        "model": "deepseek-flash",
        "temperature": 0,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": "dummy system"},
            {"role": "user", "content": "dummy prompt"},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }


@pytest.mark.parametrize(
    ("attempts", "ceiling", "deadline"),
    [
        ("0", "0", "0.001"),
        ("65", "8192", "120"),
        ("1", "524289", "120"),
        ("1", "8192", "0.1"),
        ("1", "8192", "1.0001"),
    ],
)
def test_audited_g0c_environment_accepts_exact_nonnegative_and_finite_values(
    attempts, ceiling, deadline
):
    config = runtime_meter.RuntimeMeterConfig.from_environment(
        {
            "AIGINEERING_G0C_MAX_ATTEMPTS": attempts,
            "AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING": ceiling,
            "AIGINEERING_G0C_RUN_DEADLINE_SECONDS": deadline,
        }
    )
    assert config.max_attempts == int(attempts)
    assert config.output_reservation_ceiling == int(ceiling)
    assert config.deadline_seconds == float(deadline)


def test_provider_prefixed_environment_names_are_not_g0c_aliases():
    with pytest.raises(ValueError, match="configuration is invalid"):
        runtime_meter.RuntimeMeterConfig.from_environment(
            {
                "AIGINEERING_PROVIDER_MAX_ATTEMPTS": "3",
                "AIGINEERING_PROVIDER_OUTPUT_CEILING": "24576",
                "AIGINEERING_PROVIDER_DEADLINE_SECONDS": "120",
            }
        )


def test_typed_session_and_transport_constructors_support_injected_seams():
    class Clock:
        def __call__(self):
            return 100.0

    class Observer:
        warning = None

        def __init__(self):
            self.events = []

        def emit(self, name, **fields):
            self.events.append((name, fields))

    config = runtime_meter.RuntimeMeterConfig(3, 24576, 120.0)
    session = runtime_meter.RuntimeMeterSession.from_config(config, clock=Clock())
    session.api_key = "dummy-g0c-key"
    root = {"choices": [{"message": {"content": "{}"}}], "usage": None}
    calls = []

    def fake_transport(*, url, headers, body, timeout_seconds, max_response_bytes):
        calls.append((url, headers, body, timeout_seconds, max_response_bytes))
        return runtime_meter.TransportReply(200, json.dumps(root).encode())

    observer = Observer()
    transport = runtime_meter.RuntimeMeteredTransport(
        session,
        "planner",
        transport=fake_transport,
        observer_getter=lambda: observer,
    )
    result = transport(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": "Bearer dummy-g0c-key", "Content-Type": "application/json"},
        _planner_payload(),
    )
    assert result == root
    assert len(calls) == 1
    assert [name for name, _ in observer.events] == [
        "provider.permit_admitted",
        "provider.transport_entered",
        "provider.response_observed",
        "provider.attempt_finalized",
    ]


def test_process_session_is_shared_and_configuration_change_does_not_reset(monkeypatch):
    _env(monkeypatch)
    session = runtime_meter.get_process_session()
    assert runtime_meter.get_process_session() is session
    assert isinstance(session.meter, RunMeter)
    monkeypatch.setenv("AIGINEERING_G0C_MAX_ATTEMPTS", "4")
    with pytest.raises(ValueError, match="configuration changed"):
        runtime_meter.get_process_session()
    monkeypatch.setenv("AIGINEERING_G0C_MAX_ATTEMPTS", "3")
    assert runtime_meter.get_process_session() is session


def test_factory_pins_only_selected_dummy_key_and_fixed_llm_profile(monkeypatch):
    _env(monkeypatch)
    from examples.thermal_calibration.runtime_workers import create_metered_planner

    worker = create_metered_planner(worker_id="calibration-planner")
    assert worker.api_key == "dummy-g0c-key"
    assert worker._max_retries == 0
    assert worker._response_format == "json_object"
    assert worker._thinking_mode == "disabled"
    assert worker._transport.session is runtime_meter.get_process_session()
    assert "dummy-g0c-key" not in repr(runtime_meter.get_process_session())


def test_researcher_factory_and_fingerprint_bind_disabled_profile(monkeypatch):
    _env(monkeypatch)
    from examples.thermal_calibration.runtime_workers import create_metered_researcher

    worker = create_metered_researcher(worker_id="calibration-researcher")
    session = runtime_meter.get_process_session()
    assert worker._thinking_mode == "disabled"
    assert worker._response_format == ""
    assert worker._max_retries == 0
    assert worker._max_output_tokens == 8192
    assert worker._tool_definitions == session.tool_definitions
    assert worker._transport.session is session
    assert session.config.profile_version == "g0c-runtime-v2"

    config = session.config
    expected = {
        "attempts": config.max_attempts,
        "ceiling": config.output_reservation_ceiling,
        "deadline": config.deadline_seconds,
        "url": config.base_url,
        "model": config.model,
        "max_output_tokens": config.max_output_tokens,
        "timeout": config.request_timeout_seconds,
        "tool_registry_ref": config.tool_registry_ref,
        "profile_version": "g0c-runtime-v2",
        "api_key_env": config.api_key_env,
        "planner_id": "calibration-planner",
        "researcher_id": "calibration-researcher",
        "retry_count": 0,
        "planner_thinking": "disabled",
        "researcher_thinking": "disabled",
        "planner_response_format": "json_object",
    }
    expected_fingerprint = hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    assert session.fingerprint == expected_fingerprint


def test_planner_and_researcher_transports_share_one_meter_without_reset(monkeypatch):
    _env(monkeypatch, attempts="2", ceiling="16384")
    session, _key, planner = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    researcher_session, _key, researcher = runtime_meter.create_transport(
        "researcher", "calibration-researcher"
    )
    assert researcher_session is session
    response = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 2},
    }
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(
            200, json.dumps(response).encode()
        ),
    )
    planner(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": "Bearer dummy-g0c-key", "Content-Type": "application/json"},
        _planner_payload(),
    )
    researcher_payload = _planner_payload()
    researcher_payload.pop("response_format")
    researcher_payload["thinking"] = {"type": "disabled"}
    researcher(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": "Bearer dummy-g0c-key", "Content-Type": "application/json"},
        researcher_payload,
    )
    assert session.phase == "preparing"  # injected transports do not construct Workers
    summary = session.meter.snapshot()
    assert summary.admitted_attempts == 2
    assert [row.permit_id for row in summary.attempts] == [1, 2]
    assert [row.prompt_tokens.value for row in summary.attempts] == [2, 2]


def test_concurrent_duplicate_role_construction_poisons_one_epoch(monkeypatch):
    _env(monkeypatch)
    barrier = Barrier(2)

    def construct():
        barrier.wait()
        try:
            from examples.thermal_calibration.runtime_workers import (
                create_metered_planner,
            )

            return create_metered_planner(worker_id="calibration-planner")
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: construct(), range(2)))
    assert sum(not isinstance(result, ValueError) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    session = runtime_meter.get_process_session()
    assert session.poisoned
    assert session.phase == "poisoned"
    assert session.meter.snapshot().admitted_attempts == 0


def test_transport_passes_raw_usage_and_returns_original_root(monkeypatch):
    _env(monkeypatch)
    _session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    response = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 7},
    }
    seen = {}

    def fake_transport(**kwargs):
        seen.update(kwargs)
        return runtime_meter.TransportReply(200, json.dumps(response).encode())

    monkeypatch.setattr(runtime_meter, "_stdlib_https_transport", fake_transport)
    returned = transport(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": "Bearer dummy-g0c-key", "Content-Type": "application/json"},
        _planner_payload(),
    )
    assert returned == response
    record = _session.meter.snapshot().attempts[0]
    assert record.outcome == "response"
    assert record.prompt_tokens.value == 7
    assert record.completion_tokens.status == "missing"
    assert seen["timeout_seconds"] == 60


@pytest.mark.parametrize(
    ("usage", "expected_prompt", "expected_completion"),
    [
        (True, "invalid", "invalid"),
        ([1, 2], "invalid", "invalid"),
        ("tokens", "invalid", "invalid"),
        (17, "invalid", "invalid"),
        ({"prompt_tokens": 4}, "reported", "missing"),
        (None, "not_returned", "not_returned"),
    ],
)
def test_transport_passes_each_raw_usage_shape_to_g0a(
    monkeypatch, usage, expected_prompt, expected_completion
):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    response = {"choices": [{"message": {"content": "{}"}}], "usage": usage}
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(
            200, json.dumps(response).encode()
        ),
    )
    transport(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": "Bearer dummy-g0c-key", "Content-Type": "application/json"},
        _planner_payload(),
    )
    attempt = session.meter.snapshot().attempts[0]
    assert attempt.prompt_tokens.status == expected_prompt
    assert attempt.completion_tokens.status == expected_completion


def test_complete_non_object_response_is_not_a_usable_model_mapping(monkeypatch):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(200, b"[]"),
    )
    with pytest.raises(WorkerExecutionError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value.code == "provider_invalid_response"
    attempt = session.meter.snapshot().attempts[0]
    assert attempt.outcome == "response"
    assert attempt.prompt_tokens.status == "not_returned"


def test_complete_http_error_retains_g0a_usage_and_original_status(monkeypatch):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    root = {"error": "dummy", "usage": {"prompt_tokens": 3}}
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(503, json.dumps(root).encode()),
    )
    with pytest.raises(runtime_meter.ProviderError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value.status_code == 503
    attempt = session.meter.snapshot().attempts[0]
    assert attempt.outcome == "failed"
    assert attempt.prompt_tokens.value == 3
    assert attempt.completion_tokens.status == "missing"


def test_late_complete_2xx_retains_usage_but_never_returns_root(monkeypatch):
    class Clock:
        def __init__(self):
            self.values = iter((0.0, 1.0, 2.0, 10.0, 11.0, 12.0))

        def __call__(self):
            return next(self.values)

    _env(monkeypatch, deadline="5")
    monkeypatch.setattr(runtime_meter, "_monotonic_clock", Clock())
    runtime_meter.reset_process_session_for_tests()
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    root = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 8},
    }
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(200, json.dumps(root).encode()),
    )
    with pytest.raises(WorkerExecutionError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value.code == "provider_deadline_exceeded"
    attempt = session.meter.snapshot().attempts[0]
    assert attempt.outcome == "response"
    assert attempt.prompt_tokens.value == 8
    assert attempt.late is True
    assert attempt.result_eligible is False


def test_transport_denial_does_not_enter_transport(monkeypatch):
    _env(monkeypatch, ceiling="8192")
    _session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    called = []
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: (
            called.append(kwargs)
            or runtime_meter.TransportReply(200, b'{"usage":null}')
        ),
    )
    transport(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": "Bearer dummy-g0c-key", "Content-Type": "application/json"},
        _planner_payload(),
    )
    with pytest.raises(Exception, match="budget exhausted"):
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert len(called) == 1
    assert _session.meter.snapshot().admitted_attempts == 1


def test_transport_failure_is_finalized_without_response_or_raw_error(monkeypatch):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )

    def fail(**kwargs):
        raise OSError("dummy prompt and secret must not escape")

    monkeypatch.setattr(runtime_meter, "_stdlib_https_transport", fail)
    with pytest.raises(
        runtime_meter.ProviderError, match="provider transport error"
    ) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert str(caught.value) == "provider transport error"
    record = session.meter.snapshot().attempts[0]
    assert record.outcome == "failed"
    assert record.prompt_tokens.status == "not_returned"


def test_unknown_exception_after_decode_finalizes_once_with_observed_usage(
    monkeypatch,
):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    root = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 6},
    }
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(200, json.dumps(root).encode()),
    )
    sentinel = RuntimeError("unknown test failure")
    calls = 0

    def fail_late_check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sentinel
        return float(calls)

    monkeypatch.setattr(session, "clock", fail_late_check)
    with pytest.raises(RuntimeError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value is sentinel
    attempt = session.meter.snapshot().attempts[0]
    assert attempt.outcome == "response"
    assert attempt.prompt_tokens.value == 6


def test_postdecode_clock_io_failure_keeps_http_class_and_observed_usage(monkeypatch):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    response = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 7},
    }
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(
            200, json.dumps(response).encode()
        ),
    )
    calls = 0

    def fail_postdecode_clock_once():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("dummy-clock-secret-marker")
        return 100.0

    monkeypatch.setattr(session, "clock", fail_postdecode_clock_once)
    observer = type(
        "Observer",
        (),
        {"warning": None, "emit": lambda self, name, **fields: None},
    )()
    monkeypatch.setattr(runtime_meter, "current_observer", lambda: observer)
    with pytest.raises(WorkerExecutionError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value.code == "provider_internal_error"
    assert "dummy-clock-secret-marker" not in str(caught.value)
    attempt = session.meter.snapshot().attempts[0]
    assert attempt.outcome == "response"
    assert attempt.prompt_tokens.status == "reported"
    assert attempt.prompt_tokens.value == 7


def test_observer_getter_failure_is_auxiliary_and_preserves_response(monkeypatch):
    class Observer:
        warning = None

        def emit(self, name, **fields):
            pass

    _env(monkeypatch)
    session, _key, _transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    response = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 7},
    }
    calls = []

    def fake_transport(**kwargs):
        calls.append(kwargs)
        return runtime_meter.TransportReply(200, json.dumps(response).encode())

    observer = Observer()
    lookups = 0

    def fail_response_observer_lookup():
        nonlocal lookups
        lookups += 1
        if lookups == 3:
            raise RuntimeError("dummy observer lookup marker")
        return observer

    transport = runtime_meter.RuntimeMeteredTransport(
        session,
        "planner",
        transport=fake_transport,
        observer_getter=fail_response_observer_lookup,
    )
    result = transport(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": "Bearer dummy-g0c-key", "Content-Type": "application/json"},
        _planner_payload(),
    )
    assert result == response
    assert len(calls) == 1
    assert lookups == 4
    assert session.meter.snapshot().attempts[0].prompt_tokens.value == 7


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"{", "invalid_json"),
        (b"[]", "invalid_response"),
        (b"x" * (4 * 1024 * 1024 + 1), "response_too_large"),
    ],
    ids=["invalid-json", "nonobject-root", "oversized"],
)
def test_late_protocol_event_keeps_protocol_error_precedence(
    monkeypatch, body, expected_code
):
    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    _env(monkeypatch)
    clock = Clock()
    monkeypatch.setattr(runtime_meter, "_monotonic_clock", clock)
    runtime_meter.reset_process_session_for_tests()
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )

    class Observer:
        warning = None

        def __init__(self):
            self.events = []

        def emit(self, name, **fields):
            self.events.append((name, fields))

    observer = Observer()
    monkeypatch.setattr(runtime_meter, "current_observer", lambda: observer)

    def late_reply(**kwargs):
        clock.now = 221.0
        return runtime_meter.TransportReply(200, body)

    monkeypatch.setattr(runtime_meter, "_stdlib_https_transport", late_reply)
    with pytest.raises(WorkerExecutionError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert (
        caught.value.code
        == {
            "invalid_json": "provider_invalid_json",
            "invalid_response": "provider_invalid_response",
            "response_too_large": "provider_response_too_large",
        }[expected_code]
    )
    final = [
        fields
        for name, fields in observer.events
        if name == "provider.attempt_finalized"
    ]
    assert final[-1]["late"] is True
    assert final[-1]["result_eligible"] is False
    assert final[-1]["error_code"] == expected_code
    assert session.meter.snapshot().attempts[0].late is True


def test_unknown_finish_error_keeps_pending_permit_and_is_not_retried(monkeypatch):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    root = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 6},
    }
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(200, json.dumps(root).encode()),
    )
    sentinel = RuntimeError("finish failed")
    calls = 0

    def failed_finish(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise sentinel

    monkeypatch.setattr(session.meter, "finish", failed_finish)
    with pytest.raises(RuntimeError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value is sentinel
    assert calls == 1
    snapshot = session.meter.snapshot()
    assert snapshot.pending_count == 1
    assert snapshot.attempts[0].prompt_tokens.status == "pending"


@pytest.mark.parametrize("commit_before_secondary_failure", [False, True])
def test_cleanup_finish_failure_preserves_original_unknown_exception(
    monkeypatch, commit_before_secondary_failure
):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    reply = runtime_meter.TransportReply(200, b"complete body")
    monkeypatch.setattr(
        runtime_meter, "_stdlib_https_transport", lambda **kwargs: reply
    )
    original = RuntimeError("original decoder sentinel")
    secondary = RuntimeError("secondary cleanup sentinel")
    finish_calls = 0
    original_finish = session.meter.finish

    def fail_once_after_decoder(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        if commit_before_secondary_failure:
            original_finish(*args, **kwargs)
        raise secondary

    monkeypatch.setattr(
        runtime_meter, "_decode_root", lambda _body: (_ for _ in ()).throw(original)
    )
    monkeypatch.setattr(session.meter, "finish", fail_once_after_decoder)
    with pytest.raises(RuntimeError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value is original
    assert finish_calls == 1
    snapshot = session.meter.snapshot()
    assert snapshot.pending_count == (0 if commit_before_secondary_failure else 1)


@pytest.mark.parametrize(
    ("status", "expected_outcome", "expected_code"),
    [(200, "response", "internal_error"), (503, "failed", "http_status")],
)
def test_unknown_decoder_error_after_complete_reply_finalizes_http_class_once(
    monkeypatch, status, expected_outcome, expected_code
):
    _env(monkeypatch)
    session, _key, transport = runtime_meter.create_transport(
        "planner", "calibration-planner"
    )
    monkeypatch.setattr(
        runtime_meter,
        "_stdlib_https_transport",
        lambda **kwargs: runtime_meter.TransportReply(status, b"complete body"),
    )
    sentinel = RuntimeError("decoder marker")
    monkeypatch.setattr(
        runtime_meter, "_decode_root", lambda body: (_ for _ in ()).throw(sentinel)
    )

    class Observer:
        warning = None

        def __init__(self):
            self.names = []
            self.events = []

        def emit(self, name, **fields):
            self.names.append(name)
            self.events.append((name, fields))

    observer = Observer()
    monkeypatch.setattr(runtime_meter, "current_observer", lambda: observer)
    finish_calls = 0
    original_finish = session.meter.finish

    def counted_finish(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(session.meter, "finish", counted_finish)
    with pytest.raises(RuntimeError) as caught:
        transport(
            "https://api.deepseek.com/v1/chat/completions",
            {
                "Authorization": "Bearer dummy-g0c-key",
                "Content-Type": "application/json",
            },
            _planner_payload(),
        )
    assert caught.value is sentinel
    assert finish_calls == 1
    attempt = session.meter.snapshot().attempts[0]
    assert attempt.outcome == expected_outcome
    assert attempt.prompt_tokens.status == "not_returned"
    assert "provider.response_observed" not in observer.names
    final_event, final_fields = observer.events[-1]
    assert final_event == "provider.attempt_finalized"
    assert final_fields["status_code"] == status
    assert final_fields["error_code"] == expected_code
