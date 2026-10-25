"""Fake-transport coverage for the opt-in native provider adapter."""

from __future__ import annotations

import http.client
import io
import json
import pytest

from examples.thermal_calibration.deepseek_client import (
    DeepSeekChatCompletionClient,
    DeepSeekConfig,
    DirectLiveLimits,
    SafeProviderError,
    TransportReply,
    _NoRedirectHandler,
    _stdlib_https_transport,
)
from examples.thermal_calibration.provider_meter import RunBudget, RunMeter


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
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
        max_arguments_bytes=1024,
        max_final_bytes=1024,
        max_http_retries=0,
        retry_backoff_seconds=0.25,
    )
    values.update(overrides)
    return DirectLiveLimits(**values)


def _config(**overrides):
    values = dict(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-flash",
        thinking_mode="disabled",
    )
    values.update(overrides)
    return DeepSeekConfig(**values)


def _body(*, usage="sentinel", finish_reason="stop", message=None, extra=None):
    if message is None:
        message = {"role": "assistant", "content": "{}", "tool_calls": []}
    value = {
        "id": "response-1",
        "object": "chat.completion",
        "created": 1,
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
    if usage != "sentinel":
        value["usage"] = usage
    if extra:
        value.update(extra)
    return json.dumps(value, separators=(",", ":")).encode()


class QueueTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _FakeHTTPConnectionSocket:
    def __init__(self, wire):
        self._wire = wire

    def makefile(self, *_args, **_kwargs):
        return io.BytesIO(self._wire)


class _HTTPResponseOpener:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def open(self, *_args, **_kwargs):
        self.calls += 1
        return self._responses.pop(0)


def _stdlib_response(status, body, framing):
    if framing == "fixed":
        wire = (
            f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body) + 97}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
    elif framing == "chunked":
        wire = (
            f"HTTP/1.1 {status} Test\r\nTransfer-Encoding: chunked\r\n"
            "Connection: close\r\n\r\n"
            f"{len(body) + 97:x}\r\n"
        ).encode() + body
    else:
        wire = (
            f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
    response = http.client.HTTPResponse(_FakeHTTPConnectionSocket(wire))
    response.begin()
    return response


def _client(
    transport, *, clock=None, limits=None, sink=None, sleeper=None, mode="disabled"
):
    clock = clock or FakeClock()
    limits = limits or _limits()
    meter = RunMeter(
        RunBudget(
            max_attempts=limits.max_attempts,
            max_output_tokens_per_request=limits.max_output_tokens_per_request,
            output_reservation_ceiling=limits.output_reservation_ceiling,
            deadline_seconds=limits.deadline_seconds,
        ),
        clock=clock,
    )
    events = []
    client = DeepSeekChatCompletionClient(
        _config(thinking_mode=mode),
        secret="test-secret-do-not-leak",
        limits=limits,
        meter=meter,
        transport=transport,
        clock=clock,
        sleeper=sleeper or clock.sleep,
        event_sink=sink or events.append,
    )
    return client, meter, clock, events


def test_request_is_frozen_native_shape_and_reply_is_typed():
    transport = QueueTransport(
        TransportReply(
            status=200,
            body=_body(
                usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
            ),
        )
    )
    client, meter, _, events = _client(transport)
    result = client.complete(
        messages=[{"role": "user", "content": "secret prompt"}],
        tools=[],
        timeout_seconds=3.0,
    )

    assert result.finish_reason == "stop"
    request = json.loads(transport.calls[0]["body"])
    assert set(request) == {
        "model",
        "messages",
        "tools",
        "max_tokens",
        "thinking",
        "stream",
    }
    assert "n" not in request
    assert request["stream"] is False
    assert request["max_tokens"] == 64
    assert request["thinking"] == {"type": "disabled"}
    assert transport.calls[0]["timeout_seconds"] == 3.0
    assert transport.calls[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert meter.snapshot().attempts[0].prompt_tokens.value == 2
    assert [item.event_type for item in events] == [
        "permit_admitted",
        "transport_entered",
        "response_observed",
        "permit_finalized",
    ]
    assert "test-secret-do-not-leak" not in repr(client)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.deepseek.com/v1",
        "https://api.deepseek.com:443/v1",
        "https://user:pass@api.deepseek.com/v1",
        "https://api.deepseek.com/v1?token=secret",
        "https://api.deepseek.com/v1/../other",
        "https://api.example.com/v1",
    ],
)
def test_provider_url_rejects_noncanonical_or_unsafe_values(base_url):
    with pytest.raises(ValueError):
        _config(base_url=base_url)


@pytest.mark.parametrize(
    "usage,expected_statuses,expected_values",
    [
        (
            {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15},
            ("reported", "reported", "reported"),
            (7, 8, 15),
        ),
        ({"prompt_tokens": 7}, ("reported", "missing", "missing"), (7, None, None)),
        (
            {"prompt_tokens": -1, "completion_tokens": 2},
            ("invalid", "reported", "missing"),
            (None, 2, None),
        ),
        (None, ("not_returned",) * 3, (None,) * 3),
        (True, ("invalid",) * 3, (None,) * 3),
        ([1, 2], ("invalid",) * 3, (None,) * 3),
        ("usage", ("invalid",) * 3, (None,) * 3),
        (12, ("invalid",) * 3, (None,) * 3),
        (1.25, ("invalid",) * 3, (None,) * 3),
    ],
)
def test_raw_usage_values_are_forwarded_unchanged_to_meter(
    usage, expected_statuses, expected_values
):
    transport = QueueTransport(TransportReply(status=200, body=_body(usage=usage)))
    client, meter, _, _ = _client(transport)
    client.complete(messages=[], tools=[], timeout_seconds=3.0)
    attempt = meter.snapshot().attempts[0]
    components = (
        attempt.prompt_tokens,
        attempt.completion_tokens,
        attempt.total_tokens,
    )
    assert tuple(value.status for value in components) == expected_statuses
    assert tuple(value.value for value in components) == expected_values


def test_absent_usage_and_non_object_root_json_are_not_fabricated():
    transport = QueueTransport(
        TransportReply(status=200, body=_body()),
        TransportReply(status=200, body=b"[1,2]"),
    )
    client, meter, _, _ = _client(transport)
    client.complete(messages=[], tools=[], timeout_seconds=3.0)
    with pytest.raises(SafeProviderError):
        client.complete(messages=[], tools=[], timeout_seconds=3.0)
    attempts = meter.snapshot().attempts
    assert attempts[0].prompt_tokens.status == "not_returned"
    assert attempts[1].prompt_tokens.status == "not_returned"


def test_duplicate_keys_have_no_usage_and_documented_details_remain_unsupported():
    duplicate = (
        b'{"id":"x","id":"y","object":"chat.completion","created":1,'
        b'"model":"deepseek-flash","choices":[],'
        b'"usage":{"prompt_tokens":5}}'
    )
    transport = QueueTransport(
        TransportReply(status=200, body=duplicate),
        TransportReply(
            status=200,
            body=_body(
                usage={
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "total_tokens": 6,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 1},
                }
            ),
        ),
    )
    client, meter, _, _ = _client(transport)
    with pytest.raises(SafeProviderError):
        client.complete(messages=[], tools=[], timeout_seconds=3.0)
    assert meter.snapshot().attempts[0].prompt_tokens.status == "not_returned"
    client.complete(messages=[], tools=[], timeout_seconds=3.0)
    assert meter.snapshot().attempts[1].prompt_tokens.value == 5
    assert meter.snapshot().cached_reasoning_status == "unsupported"


def test_invalid_choice_keeps_decoded_usage_and_unknown_finish_is_terminal():
    usage = {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}
    raw = _body(usage=usage, extra={"unexpected": True})
    transport = QueueTransport(TransportReply(status=200, body=raw))
    client, meter, _, _ = _client(transport)
    with pytest.raises(SafeProviderError) as error:
        client.complete(messages=[], tools=[], timeout_seconds=3.0)
    assert error.value.code == "invalid_response"
    attempt = meter.snapshot().attempts[0]
    assert attempt.outcome == "response"
    assert attempt.prompt_tokens.value == 9

    transport = QueueTransport(
        TransportReply(
            status=200,
            body=_body(usage=usage, finish_reason="future_finish"),
        )
    )
    client, meter, _, _ = _client(transport)
    with pytest.raises(SafeProviderError) as error:
        client.complete(messages=[], tools=[], timeout_seconds=3.0)
    assert error.value.code == "unsupported_finish_reason"
    assert meter.snapshot().attempts[0].prompt_tokens.value == 9


def test_thinking_reasoning_is_preserved_in_next_native_request():
    first_message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "opaque continuation marker",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "tool", "arguments": "{}"},
            }
        ],
    }
    final_message = {"role": "assistant", "content": "{}", "tool_calls": []}
    transport = QueueTransport(
        TransportReply(
            status=200,
            body=_body(message=first_message, finish_reason="tool_calls", usage=None),
        ),
        TransportReply(status=200, body=_body(message=final_message, usage=None)),
    )
    client, _, _, _ = _client(transport, mode="enabled")
    response = client.complete(messages=[], tools=[], timeout_seconds=3.0)
    next_messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": response.message.reasoning_content,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "tool", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
    ]
    client.complete(messages=next_messages, tools=[], timeout_seconds=3.0)
    sent = json.loads(transport.calls[1]["body"])["messages"]
    assert sent[0]["reasoning_content"] == "opaque continuation marker"


@pytest.mark.parametrize("status", [200, 400, 429, 500, 503])
def test_decoded_non_mapping_usage_is_invalid_for_every_http_status(status):
    transport = QueueTransport(TransportReply(status=status, body=_body(usage=["raw"])))
    limits = _limits(max_http_retries=0)
    client, meter, _, events = _client(transport, limits=limits)
    if status == 200:
        client.complete(messages=[], tools=[], timeout_seconds=3.0)
    else:
        with pytest.raises(SafeProviderError):
            client.complete(messages=[], tools=[], timeout_seconds=3.0)
    attempt = meter.snapshot().attempts[0]
    assert attempt.outcome == ("response" if status == 200 else "failed")
    assert (
        attempt.prompt_tokens.status,
        attempt.completion_tokens.status,
        attempt.total_tokens.status,
    ) == ("invalid",) * 3


@pytest.mark.parametrize("status", [200, 400, 429, 500, 503])
def test_partial_mapping_usage_is_preserved_for_every_http_status(status):
    transport = QueueTransport(
        TransportReply(
            status=status,
            body=_body(usage={"prompt_tokens": 11, "completion_tokens": -1}),
        )
    )
    client, meter, _, _ = _client(transport)
    if status == 200:
        client.complete(messages=[], tools=[], timeout_seconds=3.0)
    else:
        with pytest.raises(SafeProviderError):
            client.complete(messages=[], tools=[], timeout_seconds=3.0)
    attempt = meter.snapshot().attempts[0]
    assert attempt.prompt_tokens.status == "reported"
    assert attempt.prompt_tokens.value == 11
    assert attempt.completion_tokens.status == "invalid"
    assert attempt.total_tokens.status == "missing"


def test_http_500_malformed_complete_body_retries_with_fresh_permit_and_none_usage():
    transport = QueueTransport(
        TransportReply(status=500, body=b"{malformed"),
        TransportReply(
            status=200,
            body=_body(
                usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
            ),
        ),
    )
    limits = _limits(max_http_retries=1, retry_backoff_seconds=0.5)
    client, meter, clock, events = _client(transport, limits=limits)
    client.complete(messages=[], tools=[], timeout_seconds=4.0)
    assert len(transport.calls) == 2
    assert clock.sleeps == [0.5]
    attempts = meter.snapshot().attempts
    assert attempts[0].prompt_tokens.status == "not_returned"
    assert attempts[1].prompt_tokens.value == 1
    assert [event.event_type for event in events].count("permit_admitted") == 2


@pytest.mark.parametrize(
    "remaining,backoff,expected_dispatches",
    [(2.0, 1.0, 2), (1.0, 1.0, 1), (0.5, 1.0, 1)],
)
def test_retry_backoff_never_dispatches_at_or_after_deadline(
    remaining, backoff, expected_dispatches
):
    clock = FakeClock()
    limits = _limits(
        deadline_seconds=10.0, max_http_retries=1, retry_backoff_seconds=backoff
    )

    class AdvancingFailure:
        def __init__(self):
            self.calls = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                clock.now = 100.0 + 10.0 - remaining
                return TransportReply(status=503, body=b"{}")
            return TransportReply(status=200, body=_body(usage=None))

    transport = AdvancingFailure()
    client, meter, _, _ = _client(transport, clock=clock, limits=limits)
    if expected_dispatches == 1:
        with pytest.raises(SafeProviderError):
            client.complete(messages=[], tools=[], timeout_seconds=5.0)
    else:
        client.complete(messages=[], tools=[], timeout_seconds=5.0)
    assert len(transport.calls) == expected_dispatches
    assert meter.snapshot().admitted_attempts == expected_dispatches


def test_attempt_and_reservation_denial_stop_retry_before_transport():
    for limits in (
        _limits(max_attempts=1, max_http_retries=1),
        _limits(output_reservation_ceiling=64, max_http_retries=1),
    ):
        transport = QueueTransport(TransportReply(status=503, body=b"{}"))
        client, meter, _, _ = _client(transport, limits=limits)
        with pytest.raises(SafeProviderError):
            client.complete(messages=[], tools=[], timeout_seconds=2.0)
        assert len(transport.calls) == 1
        assert meter.snapshot().admitted_attempts == 1


def test_production_reader_reads_at_most_cap_plus_one_and_redirects_are_refused():
    class Reply:
        status = 200

        def __init__(self):
            self.read_sizes = []

        def read(self, size):
            self.read_sizes.append(size)
            return b"x" * size

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    response = Reply()

    class Opener:
        def open(self, request, timeout):
            self.request = request
            self.timeout = timeout
            return response

    opener = Opener()
    transport_reply = _stdlib_https_transport(
        url="https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": "Bearer sentinel"},
        body=b"{}",
        timeout_seconds=2.0,
        max_response_bytes=5,
        opener=opener,
    )
    assert response.read_sizes == [6]
    assert len(transport_reply.body) == 6
    assert (
        _NoRedirectHandler().redirect_request(
            None, None, 302, "Found", {}, "https://elsewhere.invalid"
        )
        is None
    )
    assert opener.request.get_header("Authorization") == "Bearer sentinel"

    transport = QueueTransport(TransportReply(status=200, body=b"123456"))
    limits = _limits(max_response_bytes=5)
    client, meter, _, events = _client(transport, limits=limits)
    with pytest.raises(SafeProviderError) as error:
        client.complete(messages=[], tools=[], timeout_seconds=2.0)
    assert error.value.code == "response_too_large"
    assert meter.snapshot().attempts[0].prompt_tokens.status == "not_returned"
    assert not any(event.event_type == "response_observed" for event in events)


@pytest.mark.parametrize("status", [200, 500])
@pytest.mark.parametrize("framing", ["fixed", "chunked"])
def test_stdlib_production_reader_rejects_incomplete_http_framing_without_retry(
    status, framing
):
    partial_body = _body(
        usage={"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}
    )
    opener = _HTTPResponseOpener(
        [
            _stdlib_response(status, partial_body, framing),
            _stdlib_response(200, _body(), "complete"),
        ]
    )

    def transport(**kwargs):
        return _stdlib_https_transport(**kwargs, opener=opener)

    limits = _limits(max_http_retries=1, retry_backoff_seconds=0)
    client, meter, _, events = _client(transport, limits=limits)
    with pytest.raises(SafeProviderError) as error:
        client.complete(messages=[], tools=[], timeout_seconds=2.0)
    assert error.value.code == "transport_error"
    assert opener.calls == 1
    summary = meter.snapshot()
    assert summary.admitted_attempts == 1
    assert summary.attempts[0].outcome == "failed"
    assert all(
        getattr(summary.attempts[0], name).status == "not_returned"
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    assert not any(event.event_type == "response_observed" for event in events)


@pytest.mark.parametrize(
    "status,expected_outcome", [(200, "response"), (500, "failed")]
)
def test_stdlib_production_reader_returns_cap_plus_one_overflow_for_client(
    status, expected_outcome
):
    opener = _HTTPResponseOpener(
        [
            _stdlib_response(status, b"x" * 100, "complete"),
            _stdlib_response(200, _body(), "complete"),
        ]
    )

    def transport(**kwargs):
        return _stdlib_https_transport(**kwargs, opener=opener)

    limits = _limits(max_response_bytes=16, max_http_retries=1)
    client, meter, _, events = _client(transport, limits=limits)
    with pytest.raises(SafeProviderError) as error:
        client.complete(messages=[], tools=[], timeout_seconds=2.0)
    assert error.value.code == "response_too_large"
    assert error.value.status_code == status
    assert opener.calls == 1
    summary = meter.snapshot()
    assert summary.admitted_attempts == 1
    assert summary.attempts[0].outcome == expected_outcome
    assert summary.attempts[0].prompt_tokens.status == "not_returned"
    finalized = [event for event in events if event.event_type == "permit_finalized"]
    assert len(finalized) == 1 and finalized[0].status_code == status
    assert not any(event.event_type == "response_observed" for event in events)


def test_unexpected_transport_exception_is_not_relabelled_or_retried():
    failure = RuntimeError("private error text must not leak")
    transport = QueueTransport(failure)
    client, meter, _, events = _client(transport, limits=_limits(max_http_retries=4))
    with pytest.raises(RuntimeError) as error:
        client.complete(messages=[], tools=[], timeout_seconds=2.0)
    assert error.value is failure
    assert len(transport.calls) == 1
    assert meter.snapshot().pending_count == 1
    assert "private error text" not in repr(events)


@pytest.mark.parametrize(
    "failure,expected_code,expected_outcome",
    [
        (TimeoutError("private timeout"), "transport_timeout", "timeout"),
        (OSError("private network error"), "transport_error", "failed"),
    ],
)
def test_expected_transport_failures_finish_unknown_usage(
    failure, expected_code, expected_outcome
):
    transport = QueueTransport(failure)
    client, meter, _, _ = _client(transport)
    with pytest.raises(SafeProviderError) as error:
        client.complete(messages=[], tools=[], timeout_seconds=2.0)
    assert error.value.code == expected_code
    attempt = meter.snapshot().attempts[0]
    assert attempt.outcome == expected_outcome
    assert attempt.prompt_tokens.status == "not_returned"


@pytest.mark.parametrize("fail_on", ["permit_admitted", "response_observed"])
def test_event_sink_failure_never_retries_and_preserves_pending_permit(fail_on):
    transport = QueueTransport(TransportReply(status=200, body=_body(usage=None)))
    clock = FakeClock()
    limits = _limits(max_http_retries=3)
    meter = RunMeter(
        RunBudget(
            limits.max_attempts,
            limits.max_output_tokens_per_request,
            limits.output_reservation_ceiling,
            limits.deadline_seconds,
        ),
        clock=clock,
    )

    def sink(event):
        if event.event_type == fail_on:
            raise OSError("private sink error")

    client = DeepSeekChatCompletionClient(
        _config(),
        secret="dummy",
        limits=limits,
        meter=meter,
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        event_sink=sink,
    )
    with pytest.raises(OSError):
        client.complete(messages=[], tools=[], timeout_seconds=2.0)
    assert len(transport.calls) == (0 if fail_on == "permit_admitted" else 1)
    assert meter.snapshot().pending_count == 1


def test_post_dispatch_clock_failure_leaves_permit_pending_without_retry():
    class BrokenFinishClock:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 4:
                raise RuntimeError("private clock failure")
            return 100.0

        def sleep(self, _seconds):
            return None

    clock = BrokenFinishClock()
    transport = QueueTransport(TransportReply(status=200, body=_body(usage=None)))
    limits = _limits(max_http_retries=4)
    meter = RunMeter(
        RunBudget(
            limits.max_attempts,
            limits.max_output_tokens_per_request,
            limits.output_reservation_ceiling,
            limits.deadline_seconds,
        ),
        clock=clock,
    )
    client = DeepSeekChatCompletionClient(
        _config(),
        secret="dummy",
        limits=limits,
        meter=meter,
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
    )
    with pytest.raises(RuntimeError, match="private clock failure"):
        client.complete(messages=[], tools=[], timeout_seconds=2.0)
    assert len(transport.calls) == 1
    assert meter.snapshot().pending_count == 1
