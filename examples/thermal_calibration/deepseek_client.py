"""Opt-in bounded DeepSeek Chat Completions client for the direct Harness arm."""

from __future__ import annotations

import json
import http.client
import math
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from examples.thermal_calibration.direct_harness import (
    DirectProviderResponse,
    ProviderAssistantMessage,
    ProviderToolCall,
)
from examples.thermal_calibration.provider_meter import (
    InvalidRequestLimit,
    OutputReservationExceeded,
    RequestAttemptBudgetExceeded,
    RunDeadlineExpired,
    RunMeter,
    RunUsageSummary,
)

_FINISH_REASONS = frozenset(
    {
        "stop",
        "tool_calls",
        "length",
        "content_filter",
        "insufficient_system_resource",
        "aborted",
    }
)
_TOP_LEVEL_REQUIRED = frozenset({"id", "object", "created", "model", "choices"})
_TOP_LEVEL_OPTIONAL = frozenset({"system_fingerprint", "usage"})


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    base_url: str
    model: str
    thinking_mode: Literal["enabled", "disabled"]

    def __post_init__(self) -> None:
        _validate_provider_config(self)


@dataclass(frozen=True, slots=True)
class DirectLiveLimits:
    max_attempts: int
    max_output_tokens_per_request: int
    output_reservation_ceiling: int
    deadline_seconds: float
    request_timeout_seconds: float
    max_model_calls: int
    max_tool_attempts: int
    max_response_bytes: int
    max_arguments_bytes: int
    max_final_bytes: int
    max_http_retries: int
    retry_backoff_seconds: float

    def __post_init__(self) -> None:
        integer_fields = {
            "max_attempts": (self.max_attempts, 0, None),
            "max_output_tokens_per_request": (
                self.max_output_tokens_per_request,
                1,
                393_216,
            ),
            "output_reservation_ceiling": (self.output_reservation_ceiling, 0, None),
            "max_model_calls": (self.max_model_calls, 1, None),
            "max_tool_attempts": (self.max_tool_attempts, 1, None),
            "max_response_bytes": (self.max_response_bytes, 1, None),
            "max_arguments_bytes": (self.max_arguments_bytes, 1, None),
            "max_final_bytes": (self.max_final_bytes, 1, None),
            "max_http_retries": (self.max_http_retries, 0, None),
        }
        for name, (value, minimum, maximum) in integer_fields.items():
            if (
                type(value) is not int
                or value < minimum
                or (maximum and value > maximum)
            ):
                if maximum:
                    raise ValueError(
                        f"{name} must be an integer in {minimum}..{maximum}"
                    )
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name, value in (
            ("deadline_seconds", self.deadline_seconds),
            ("request_timeout_seconds", self.request_timeout_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError(f"{name} must be a finite positive number")
            try:
                valid = math.isfinite(value) and value > 0
            except (OverflowError, TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError(f"{name} must be a finite positive number")
        if isinstance(self.retry_backoff_seconds, bool) or not isinstance(
            self.retry_backoff_seconds, Real
        ):
            raise ValueError("retry_backoff_seconds must be finite and nonnegative")
        try:
            backoff_valid = (
                math.isfinite(self.retry_backoff_seconds)
                and self.retry_backoff_seconds >= 0
            )
        except (OverflowError, TypeError, ValueError):
            backoff_valid = False
        if not backoff_valid:
            raise ValueError("retry_backoff_seconds must be finite and nonnegative")
        if (
            self.output_reservation_ceiling
            and self.max_output_tokens_per_request > self.output_reservation_ceiling
        ):
            raise ValueError(
                "per-request output cap exceeds output reservation ceiling"
            )


@dataclass(frozen=True, slots=True)
class DirectLiveConfig:
    case_id: str
    output_dir: str
    run_id: str
    api_key_env: str
    provider: DeepSeekConfig
    limits: DirectLiveLimits


@dataclass(frozen=True, slots=True)
class ProviderAttemptEvent:
    ordinal: int
    event_type: Literal[
        "permit_admitted", "transport_entered", "response_observed", "permit_finalized"
    ]
    outcome: Literal["response", "failed", "timeout", "pending"] | None = None
    status_code: int | None = None
    error_code: str | None = None
    error_type: str | None = None
    retryable: bool | None = None
    duration_seconds: float | None = None
    finish_reason: str | None = None
    late: bool | None = None


@dataclass(frozen=True, slots=True)
class TransportReply:
    status: int
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("transport status must be an integer in 100..599")
        if not isinstance(self.body, bytes):
            raise TypeError("transport body must be bytes")


@dataclass(frozen=True, slots=True)
class DirectLiveResult:
    report: Mapping[str, object]
    oracle_result: Mapping[str, object]
    usage: RunUsageSummary
    event_count: int


class SafeProviderError(RuntimeError):
    __slots__ = ("_code", "_phase", "_status_code", "_retryable")

    def __init__(
        self,
        *,
        code: str,
        phase: str,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        allowed_codes = {
            "configuration",
            "request_budget_exhausted",
            "deadline_exceeded",
            "transport_timeout",
            "transport_error",
            "http_status",
            "response_too_large",
            "invalid_json",
            "invalid_response",
            "unsupported_finish_reason",
            "retry_exhausted",
            "internal_error",
        }
        if type(code) is not str or code not in allowed_codes:
            raise ValueError("unknown safe provider error code")
        if type(phase) is not str or phase not in {
            "configure",
            "permit",
            "dispatch",
            "read",
            "decode",
            "validate",
        }:
            raise ValueError("unknown safe provider error phase")
        if status_code is not None and (
            type(status_code) is not int or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be an HTTP status or None")
        if retryable is not None and type(retryable) is not bool:
            raise ValueError("retryable must be a bool or None")
        self._code = code
        self._phase = phase
        self._status_code = status_code
        self._retryable = retryable
        RuntimeError.__init__(self, code)

    @property
    def code(self) -> str:
        return self._code

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def status_code(self) -> int | None:
        return self._status_code

    @property
    def retryable(self) -> bool | None:
        return self._retryable

    def __repr__(self) -> str:
        return (
            f"SafeProviderError(code={self.code!r}, phase={self.phase!r}, "
            f"status_code={self.status_code!r}, retryable={self.retryable!r})"
        )


class Transport(Protocol):
    def __call__(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportReply: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _stdlib_https_transport(
    *,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
    opener=None,
) -> TransportReply:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    active_opener = opener or urllib.request.build_opener(_NoRedirectHandler())
    try:
        response = active_opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        response_body = _read_capped(response, max_response_bytes + 1)
        status = getattr(response, "status", None) or getattr(response, "code", None)
    return TransportReply(status=status, body=response_body)


def _read_capped(response, limit: int) -> bytes:
    chunks = bytearray()
    remaining = limit
    observed_eof = False
    while remaining:
        chunk = response.read(remaining)
        if not chunk:
            observed_eof = True
            break
        if not isinstance(chunk, bytes) or len(chunk) > remaining:
            raise OSError("response reader violated bounded byte contract")
        chunks.extend(chunk)
        remaining -= len(chunk)
    if observed_eof:
        expected_remaining = getattr(response, "length", None)
        if expected_remaining is not None and expected_remaining > 0:
            raise http.client.IncompleteRead(
                partial=bytes(chunks), expected=expected_remaining
            )
    return bytes(chunks)


class DeepSeekChatCompletionClient:
    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        secret: str,
        limits: DirectLiveLimits,
        meter: RunMeter,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        event_sink: Callable[[ProviderAttemptEvent], None] | None = None,
    ) -> None:
        _validate_provider_config(config)
        if not isinstance(limits, DirectLiveLimits):
            raise TypeError("limits must be DirectLiveLimits")
        if not isinstance(meter, RunMeter):
            raise TypeError("meter must be RunMeter")
        if (
            not isinstance(secret, str)
            or not secret
            or any(ch in secret for ch in "\r\n")
        ):
            raise ValueError("provider key must be nonempty safe text")
        if not callable(clock) or not callable(sleeper):
            raise TypeError("clock and sleeper must be callable")
        if transport is not None and not callable(transport):
            raise TypeError("transport must be callable")
        if event_sink is not None and not callable(event_sink):
            raise TypeError("event_sink must be callable")
        self._config = config
        self._limits = limits
        self._meter = meter
        self._authorization = f"Bearer {secret}"
        self._transport = transport or _stdlib_https_transport
        self._clock = clock
        self._sleeper = sleeper
        self._event_sink = event_sink or (lambda _event: None)
        parsed = urlsplit(config.base_url)
        prefix = "/v1" if parsed.path == "/v1" else ""
        self._url = f"https://api.deepseek.com{prefix}/chat/completions"

    def __repr__(self) -> str:
        return "DeepSeekChatCompletionClient(secret=<redacted>)"

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, object]],
        timeout_seconds: float,
    ) -> str | DirectProviderResponse:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, Real):
            raise SafeProviderError(code="configuration", phase="configure")
        try:
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise SafeProviderError(code="deadline_exceeded", phase="permit")
            request_body = json.dumps(
                {
                    "model": self._config.model,
                    "messages": messages,
                    "tools": tools,
                    "max_tokens": self._limits.max_output_tokens_per_request,
                    "thinking": {"type": self._config.thinking_mode},
                    "stream": False,
                },
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except SafeProviderError:
            raise
        except (TypeError, ValueError, OverflowError):
            raise SafeProviderError(code="invalid_response", phase="validate") from None

        retry_count = 0
        while True:
            permit = self._acquire_permit(timeout_seconds)
            ordinal = permit.permit_id
            started = self._clock()
            self._emit(ProviderAttemptEvent(ordinal, "permit_admitted"))
            self._emit(ProviderAttemptEvent(ordinal, "transport_entered"))
            try:
                reply = self._transport(
                    url=self._url,
                    headers={
                        "Authorization": self._authorization,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    body=request_body,
                    timeout_seconds=permit.timeout_seconds,
                    max_response_bytes=self._limits.max_response_bytes,
                )
            except (TimeoutError, socket.timeout):
                self._finish(
                    permit, "timeout", None, started, error_code="transport_timeout"
                )
                raise SafeProviderError(
                    code="transport_timeout", phase="dispatch", retryable=False
                ) from None
            except http.client.IncompleteRead:
                self._finish(
                    permit,
                    "failed",
                    None,
                    started,
                    error_code="transport_error",
                    retryable=False,
                )
                raise SafeProviderError(
                    code="transport_error", phase="read", retryable=False
                ) from None
            except (urllib.error.URLError, ssl.SSLError, OSError):
                self._finish(
                    permit, "failed", None, started, error_code="transport_error"
                )
                raise SafeProviderError(
                    code="transport_error", phase="dispatch", retryable=False
                ) from None

            if not isinstance(reply, TransportReply):
                raise TypeError("transport must return TransportReply")
            if len(reply.body) > self._limits.max_response_bytes:
                outcome: Literal["response", "failed"] = (
                    "response" if 200 <= reply.status < 300 else "failed"
                )
                self._finish(
                    permit,
                    outcome,
                    None,
                    started,
                    status_code=reply.status,
                    error_code="response_too_large",
                )
                raise SafeProviderError(
                    code="response_too_large",
                    phase="read",
                    status_code=reply.status,
                    retryable=False,
                )

            self._emit(
                ProviderAttemptEvent(
                    ordinal, "response_observed", status_code=reply.status
                )
            )

            root, decode_error = _decode_root(reply.body)
            usage_value = root.get("usage") if isinstance(root, dict) else None
            if not 200 <= reply.status < 300:
                self._finish(
                    permit,
                    "failed",
                    usage_value,
                    started,
                    status_code=reply.status,
                    error_code="http_status",
                    retryable=reply.status in {500, 503},
                )
                if (
                    reply.status in {500, 503}
                    and retry_count < self._limits.max_http_retries
                ):
                    self._wait_before_retry(permit)
                    retry_count += 1
                    continue
                code = (
                    "retry_exhausted" if reply.status in {500, 503} else "http_status"
                )
                raise SafeProviderError(
                    code=code,
                    phase="read",
                    status_code=reply.status,
                    retryable=False,
                )

            if decode_error is not None:
                self._finish(
                    permit,
                    "response",
                    usage_value,
                    started,
                    status_code=reply.status,
                    error_code=decode_error,
                )
                raise SafeProviderError(
                    code=decode_error, phase="decode", status_code=reply.status
                )
            try:
                provider_response = _parse_provider_response(
                    root,
                    thinking_mode=self._config.thinking_mode,
                    status_code=reply.status,
                )
            except SafeProviderError as exc:
                self._finish(
                    permit,
                    "response",
                    usage_value,
                    started,
                    status_code=reply.status,
                    error_code=exc.code,
                )
                raise
            record = self._finish(
                permit,
                "response",
                usage_value,
                started,
                status_code=reply.status,
                finish_reason=provider_response.finish_reason,
            )
            if record.late:
                raise SafeProviderError(
                    code="deadline_exceeded",
                    phase="validate",
                    status_code=reply.status,
                    retryable=False,
                )
            if not record.result_eligible:
                raise SafeProviderError(
                    code="deadline_exceeded",
                    phase="validate",
                    status_code=reply.status,
                    retryable=False,
                )
            return provider_response

    def _acquire_permit(self, timeout_seconds: float):
        try:
            return self._meter.acquire_request(
                output_cap=self._limits.max_output_tokens_per_request,
                request_timeout_cap=min(
                    self._limits.request_timeout_seconds, float(timeout_seconds)
                ),
            )
        except (RequestAttemptBudgetExceeded, OutputReservationExceeded):
            raise SafeProviderError(
                code="request_budget_exhausted", phase="permit", retryable=False
            ) from None
        except RunDeadlineExpired:
            raise SafeProviderError(
                code="deadline_exceeded", phase="permit", retryable=False
            ) from None
        except InvalidRequestLimit:
            raise SafeProviderError(code="configuration", phase="permit") from None

    def _wait_before_retry(self, permit) -> None:
        now = self._clock()
        remaining = permit.deadline_monotonic - now
        if remaining <= 0:
            raise SafeProviderError(code="deadline_exceeded", phase="permit")
        configured_backoff = float(self._limits.retry_backoff_seconds)
        delay = min(configured_backoff, remaining)
        self._sleeper(delay)
        if (
            configured_backoff >= remaining
            or self._clock() >= permit.deadline_monotonic
        ):
            raise SafeProviderError(code="deadline_exceeded", phase="permit")

    def _finish(
        self,
        permit,
        outcome: Literal["response", "failed", "timeout"],
        usage,
        started: float,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        finish_reason: str | None = None,
    ):
        record = self._meter.finish(permit, outcome=outcome, usage=usage)
        duration = self._clock() - started
        self._emit(
            ProviderAttemptEvent(
                permit.permit_id,
                "permit_finalized",
                outcome=record.outcome,
                status_code=status_code,
                error_code=error_code,
                retryable=retryable,
                duration_seconds=max(0.0, duration),
                finish_reason=finish_reason,
                late=record.late,
            )
        )
        return record

    def _emit(self, event: ProviderAttemptEvent) -> None:
        self._event_sink(event)


def _validate_provider_config(config: DeepSeekConfig) -> None:
    if not isinstance(config, DeepSeekConfig):
        raise TypeError("config must be DeepSeekConfig")
    if config.model != "deepseek-flash":
        raise ValueError("model must be deepseek-flash")
    if config.thinking_mode not in {"enabled", "disabled"}:
        raise ValueError("thinking_mode must be enabled or disabled")
    if not isinstance(config.base_url, str) or any(
        ord(char) < 32 or char.isspace() for char in config.base_url
    ):
        raise ValueError("base_url is invalid")
    parsed = urlsplit(config.base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/v1"}
    ):
        raise ValueError("base_url is invalid")


def _decode_root(body: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_float=_finite_float,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(value, dict):
        return None, "invalid_response"
    return value, None


def _parse_provider_response(
    root: dict[str, Any], *, thinking_mode: str, status_code: int
) -> DirectProviderResponse:
    allowed = _TOP_LEVEL_REQUIRED | _TOP_LEVEL_OPTIONAL
    if not _TOP_LEVEL_REQUIRED <= set(root) or set(root) - allowed:
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    if (
        root["object"] != "chat.completion"
        or not isinstance(root["id"], str)
        or not root["id"]
        or not isinstance(root["model"], str)
        or not root["model"]
        or type(root["created"]) is not int
        or root["created"] < 0
    ):
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    fingerprint = root.get("system_fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    choices = root["choices"]
    if not isinstance(choices, list) or len(choices) != 1:
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    choice = choices[0]
    if not isinstance(choice, dict) or set(choice) != {
        "index",
        "finish_reason",
        "message",
        "logprobs",
    }:
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    if (
        type(choice["index"]) is not int
        or choice["index"] != 0
        or choice["logprobs"] is not None
    ):
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    finish_reason = choice["finish_reason"]
    if not isinstance(finish_reason, str) or finish_reason not in _FINISH_REASONS:
        raise SafeProviderError(
            code="unsupported_finish_reason", phase="validate", status_code=status_code
        )
    message = choice["message"]
    if not isinstance(message, dict) or not {"role", "content"} <= set(message):
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    if set(message) - {"role", "content", "reasoning_content", "tool_calls"}:
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    if message["role"] != "assistant" or (
        message["content"] is not None and not isinstance(message["content"], str)
    ):
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    reasoning = message.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    calls_value = message.get("tool_calls", [])
    if not isinstance(calls_value, list) or len(calls_value) > 1:
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    calls: list[ProviderToolCall] = []
    if calls_value:
        call = calls_value[0]
        if not isinstance(call, dict) or set(call) != {"id", "type", "function"}:
            raise SafeProviderError(
                code="invalid_response", phase="validate", status_code=status_code
            )
        function = call["function"]
        if (
            not isinstance(call["id"], str)
            or not call["id"]
            or call["type"] != "function"
            or not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
            or not isinstance(function["name"], str)
            or not function["name"]
            or not isinstance(function["arguments"], str)
        ):
            raise SafeProviderError(
                code="invalid_response", phase="validate", status_code=status_code
            )
        calls.append(
            ProviderToolCall(
                id=call["id"], name=function["name"], arguments=function["arguments"]
            )
        )
    if finish_reason == "stop":
        if (
            calls
            or not isinstance(message["content"], str)
            or not message["content"].strip()
        ):
            raise SafeProviderError(
                code="invalid_response", phase="validate", status_code=status_code
            )
    elif finish_reason == "tool_calls":
        if len(calls) != 1 or message["content"] is not None:
            raise SafeProviderError(
                code="invalid_response", phase="validate", status_code=status_code
            )
        if thinking_mode == "enabled" and not isinstance(reasoning, str):
            raise SafeProviderError(
                code="invalid_response", phase="validate", status_code=status_code
            )
    else:
        # Structurally valid terminal provider outcomes are retained in usage,
        # then returned as typed values for the loop's terminal branch.
        pass
    if thinking_mode == "disabled" and reasoning is not None:
        raise SafeProviderError(
            code="invalid_response", phase="validate", status_code=status_code
        )
    return DirectProviderResponse(
        message=ProviderAssistantMessage(
            role="assistant",
            content=message["content"],
            tool_calls=tuple(calls),
            reasoning_content=reasoning,
        ),
        finish_reason=finish_reason,
    )


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed
