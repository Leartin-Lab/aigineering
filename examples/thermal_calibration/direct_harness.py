"""Offline-testable direct native tool-call loop; no Runtime or provider client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import time
from typing import Any, Literal, Protocol

from aigineering.core.tool_schema import (
    ToolOutputLimitError,
    ToolSchemaValidationError,
    validate_value,
)
from aigineering.core.tools import ToolRegistry
from aigineering.agent.tool_registry_loader import provider_tool_definitions
from examples.thermal_calibration.oracle import evaluate_report

_LIMIT_KEYS = frozenset(
    {
        "max_model_calls",
        "max_tool_attempts",
        "deadline_seconds",
        "max_response_bytes",
        "max_arguments_bytes",
        "max_final_bytes",
    }
)
_ENVELOPE_KEYS = frozenset({"message", "usage"})
_MESSAGE_KEYS = frozenset({"role", "content", "tool_calls"})
_CALL_KEYS = frozenset({"id", "type", "function"})
_FUNCTION_KEYS = frozenset({"name", "arguments"})
_TOOL_ERROR_CODE = "transient_tool_failure"


class DirectHarnessError(RuntimeError):
    """A safe, typed protocol or bounded-execution failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DirectTransientToolError(Exception):
    """Only this explicitly injected tool fault is returned to the model."""


class ModelClient(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, object]],
        timeout_seconds: float,
    ) -> str | DirectProviderResponse: ...


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ProviderAssistantMessage:
    role: Literal["assistant"]
    content: str | None
    tool_calls: tuple[ProviderToolCall, ...]
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class DirectProviderResponse:
    message: ProviderAssistantMessage
    finish_reason: Literal[
        "stop",
        "tool_calls",
        "length",
        "content_filter",
        "insufficient_system_resource",
        "aborted",
    ]


@dataclass(frozen=True)
class DirectRunResult:
    report: dict[str, Any]
    oracle_result: dict[str, Any]
    messages: tuple[dict[str, Any], ...]
    model_calls: int
    tool_attempts: int


def run_direct(
    *,
    client: ModelClient,
    case: Mapping[str, Any],
    standard: Mapping[str, Any],
    brief: str,
    registry: ToolRegistry,
    limits: Mapping[str, Any],
    event_sink: Callable[[dict[str, Any]], None],
    monotonic: Callable[[], float] = time.monotonic,
    response_protocol: Literal["offline-v1", "deepseek-chat-v1"] = "offline-v1",
) -> DirectRunResult:
    """Run one direct tool-call conversation and check its final report."""
    checked_limits = _validate_limits(limits)
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("brief must be nonempty text")
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    if not callable(event_sink):
        raise TypeError("event_sink must be callable")
    if response_protocol not in {"offline-v1", "deepseek-chat-v1"}:
        raise ValueError("unsupported direct response protocol")

    start = monotonic()
    deadline = start + checked_limits["deadline_seconds"]
    model_calls = 0
    tool_attempts = 0
    used_ids: set[str] = set()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": brief},
        {
            "role": "user",
            "content": _canonical_json(
                {"case": dict(case), "standard": dict(standard)}
            ),
        },
    ]
    _event(event_sink, "run_started", limits=checked_limits)

    try:
        tool_definitions = provider_tool_definitions(registry)
        while True:
            remaining = _remaining(deadline, monotonic())
            if remaining <= 0:
                raise DirectHarnessError(
                    "deadline_exceeded", "cooperative deadline expired"
                )
            if model_calls >= checked_limits["max_model_calls"]:
                raise DirectHarnessError("model_call_limit", "model call limit reached")
            model_calls += 1
            _event(
                event_sink,
                "model_request",
                ordinal=model_calls,
                remaining_timeout_seconds=remaining,
            )
            try:
                raw = client.complete(
                    messages=deepcopy(messages),
                    tools=deepcopy(tool_definitions),
                    timeout_seconds=remaining,
                )
            except TimeoutError as exc:
                _event(event_sink, "client_timeout", ordinal=model_calls)
                raise DirectHarnessError(
                    "client_timeout", "client call timed out"
                ) from exc
            except Exception as exc:
                _event(
                    event_sink,
                    "client_error",
                    ordinal=model_calls,
                    error_type=type(exc).__name__,
                )
                raise

            if _remaining(deadline, monotonic()) <= 0:
                raise DirectHarnessError(
                    "deadline_exceeded", "cooperative deadline expired"
                )
            if response_protocol == "offline-v1":
                response = _parse_response(raw, checked_limits["max_response_bytes"])
            else:
                response = _provider_response(raw)
            message = response["message"]
            tool_calls = message["tool_calls"]
            _event(
                event_sink,
                "model_response",
                ordinal=model_calls,
                kind_detail=("tool" if tool_calls else "final"),
            )

            if not tool_calls:
                final_text = message["content"]
                if not isinstance(final_text, str) or not final_text.strip():
                    raise DirectHarnessError(
                        "protocol", "final response must be nonempty JSON text"
                    )
                if len(final_text.encode("utf-8")) > checked_limits["max_final_bytes"]:
                    raise DirectHarnessError(
                        "final_output_limit", "final report exceeds byte limit"
                    )
                report = _parse_object(final_text, phase="final report")
                oracle_result = evaluate_report(case, standard, report)
                messages.append(dict(message))
                _event(
                    event_sink,
                    "run_completed",
                    oracle_passed=oracle_result.get("passed") is True,
                    within_tolerance=oracle_result.get("within_tolerance"),
                    model_calls=model_calls,
                    tool_attempts=tool_attempts,
                )
                return DirectRunResult(
                    report=report,
                    oracle_result=oracle_result,
                    messages=tuple(messages),
                    model_calls=model_calls,
                    tool_attempts=tool_attempts,
                )

            # Preserve the exact validated assistant message before any execution/refusal.
            messages.append(dict(message))
            call = tool_calls[0]
            call_id = call["id"]
            if call_id in used_ids:
                raise DirectHarnessError("protocol", "duplicate tool-call ID")
            used_ids.add(call_id)
            function = call["function"]
            name = function["name"]
            arguments_text = function["arguments"]
            if (
                len(arguments_text.encode("utf-8"))
                > checked_limits["max_arguments_bytes"]
            ):
                raise DirectHarnessError(
                    "tool_arguments", "tool arguments exceed byte limit"
                )
            try:
                arguments = _parse_object(arguments_text, phase="tool arguments")
            except DirectHarnessError as exc:
                raise DirectHarnessError(
                    "tool_arguments", "tool arguments are not strict JSON"
                ) from exc
            spec = registry.get_spec(name)
            if spec is None:
                raise DirectHarnessError("protocol", "requested tool is not registered")
            try:
                validate_value(arguments, spec.input_schema, path="$.input")
            except ToolSchemaValidationError as exc:
                _event(event_sink, "tool_input_schema", error_type=type(exc).__name__)
                raise DirectHarnessError(
                    "tool_input_schema", "tool arguments fail input schema"
                ) from exc

            if tool_attempts >= checked_limits["max_tool_attempts"]:
                raise DirectHarnessError(
                    "tool_attempt_limit", "tool attempt limit reached"
                )
            tool_attempts += 1
            if _remaining(deadline, monotonic()) <= 0:
                raise DirectHarnessError(
                    "deadline_exceeded", "cooperative deadline expired"
                )
            try:
                result_text = registry.run(name, arguments)
            except DirectTransientToolError:
                tool_result = {
                    "ok": False,
                    "result": None,
                    "error_code": _TOOL_ERROR_CODE,
                }
                _event(
                    event_sink,
                    "tool_transient_failure",
                    ordinal=tool_attempts,
                    tool=name,
                    tool_call_id=call_id,
                    error_type="DirectTransientToolError",
                )
            except ToolSchemaValidationError as exc:
                _event(event_sink, "registry_validation", error_type=type(exc).__name__)
                raise DirectHarnessError(
                    "registry_validation", "registry input validation failed"
                ) from exc
            except ToolOutputLimitError as exc:
                _event(event_sink, "tool_output_limit", error_type=type(exc).__name__)
                raise DirectHarnessError(
                    "tool_output_limit", "registry output exceeds limit"
                ) from exc
            except Exception as exc:
                _event(
                    event_sink,
                    "tool_handler_error",
                    ordinal=tool_attempts,
                    tool=name,
                    tool_call_id=call_id,
                    error_type=type(exc).__name__,
                )
                raise
            else:
                if not isinstance(result_text, str):
                    raise TypeError("ToolRegistry returned a non-text result")
                tool_result = {"ok": True, "result": result_text, "error_code": None}
                _event(
                    event_sink,
                    "tool_success",
                    ordinal=tool_attempts,
                    tool=name,
                    tool_call_id=call_id,
                    result_bytes=len(result_text.encode("utf-8")),
                )
            if _remaining(deadline, monotonic()) <= 0:
                raise DirectHarnessError(
                    "deadline_exceeded", "cooperative deadline expired"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _canonical_json(tool_result),
                }
            )
    except DirectHarnessError as exc:
        _event(
            event_sink,
            "run_failed",
            error_code=exc.code,
            error_type=type(exc).__name__,
            model_calls=model_calls,
            tool_attempts=tool_attempts,
        )
        raise
    except Exception as exc:
        # Record only safe type/phase; preserve the original failure for callers.
        _event(
            event_sink,
            "run_failed",
            error_code="unclassified",
            error_type=type(exc).__name__,
            model_calls=model_calls,
            tool_attempts=tool_attempts,
        )
        raise


def _validate_limits(limits: Mapping[str, Any]) -> dict[str, int | float]:
    if not isinstance(limits, Mapping) or set(limits) != _LIMIT_KEYS:
        raise ValueError("limits must contain the exact configured keys")
    result: dict[str, int | float] = dict(limits)
    for key in (
        "max_model_calls",
        "max_tool_attempts",
        "max_response_bytes",
        "max_arguments_bytes",
        "max_final_bytes",
    ):
        value = result[key]
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} must be a positive integer")
    value = result["deadline_seconds"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("deadline_seconds must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("deadline_seconds must be a positive finite number")
    return result


def _remaining(deadline: float, now: float) -> float:
    return deadline - now


def _parse_response(raw: Any, byte_limit: int) -> dict[str, Any]:
    if isinstance(raw, DirectProviderResponse):
        raise DirectHarnessError(
            "protocol", "provider response requires explicit deepseek-chat-v1 opt-in"
        )
    if not isinstance(raw, str):
        raise DirectHarnessError("protocol", "client response must be JSON text")
    if len(raw.encode("utf-8")) > byte_limit:
        raise DirectHarnessError("response_limit", "client response exceeds byte limit")
    try:
        envelope = _parse_object(raw, phase="response envelope")
    except DirectHarnessError:
        raise
    if set(envelope) != _ENVELOPE_KEYS or envelope["usage"] is not None:
        raise DirectHarnessError(
            "protocol", "response envelope has unsupported shape or usage"
        )
    message = envelope["message"]
    if not isinstance(message, dict) or set(message) != _MESSAGE_KEYS:
        raise DirectHarnessError("protocol", "assistant message has unsupported shape")
    if message["role"] != "assistant":
        raise DirectHarnessError("protocol", "response role must be assistant")
    calls = message["tool_calls"]
    if not isinstance(calls, list):
        raise DirectHarnessError("protocol", "tool_calls must be an array")
    if len(calls) > 1:
        raise DirectHarnessError("protocol", "exactly one tool call is supported")
    if not calls:
        if not isinstance(message["content"], str) or not message["content"].strip():
            raise DirectHarnessError("protocol", "final content must be nonempty text")
        return envelope
    if message["content"] is not None:
        raise DirectHarnessError("protocol", "tool-call content must be null")
    call = calls[0]
    if not isinstance(call, dict) or set(call) != _CALL_KEYS:
        raise DirectHarnessError("protocol", "tool call has unsupported shape")
    if not isinstance(call["id"], str) or not call["id"].strip():
        raise DirectHarnessError("protocol", "tool-call ID must be nonempty text")
    if call["type"] != "function":
        raise DirectHarnessError("protocol", "tool call type must be function")
    function = call["function"]
    if not isinstance(function, dict) or set(function) != _FUNCTION_KEYS:
        raise DirectHarnessError("protocol", "function call has unsupported shape")
    if not isinstance(function["name"], str) or not function["name"]:
        raise DirectHarnessError("protocol", "tool name must be nonempty text")
    if not isinstance(function["arguments"], str):
        raise DirectHarnessError("protocol", "tool arguments must be JSON text")
    return envelope


def _provider_response(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, DirectProviderResponse):
        raise DirectHarnessError(
            "protocol", "deepseek-chat-v1 requires a typed provider response"
        )
    if type(raw.finish_reason) is not str or raw.finish_reason not in {
        "stop",
        "tool_calls",
        "length",
        "content_filter",
        "insufficient_system_resource",
        "aborted",
    }:
        raise DirectHarnessError(
            "unsupported_finish_reason", "unsupported finish reason"
        )
    if raw.finish_reason not in {"stop", "tool_calls"}:
        raise DirectHarnessError(
            "unsupported_finish_reason", "provider finish reason is terminal"
        )
    message = raw.message
    if not isinstance(message, ProviderAssistantMessage) or message.role != "assistant":
        raise DirectHarnessError("protocol", "provider assistant message is invalid")
    if message.content is not None and not isinstance(message.content, str):
        raise DirectHarnessError("protocol", "provider content must be text or null")
    if not isinstance(message.tool_calls, tuple) or len(message.tool_calls) > 1:
        raise DirectHarnessError("protocol", "provider supports at most one tool call")
    if message.reasoning_content is not None and not isinstance(
        message.reasoning_content, str
    ):
        raise DirectHarnessError("protocol", "provider reasoning content is invalid")
    if raw.finish_reason == "stop":
        if message.tool_calls:
            raise DirectHarnessError(
                "protocol", "stop finish reason cannot include tool calls"
            )
        if not isinstance(message.content, str) or not message.content.strip():
            raise DirectHarnessError("protocol", "final content must be nonempty text")
    else:
        if len(message.tool_calls) != 1 or message.content is not None:
            raise DirectHarnessError(
                "protocol",
                "tool_calls finish reason requires one call and null content",
            )
    for call in message.tool_calls:
        if (
            not isinstance(call, ProviderToolCall)
            or not isinstance(call.id, str)
            or not call.id
            or not isinstance(call.name, str)
            or not call.name
            or not isinstance(call.arguments, str)
        ):
            raise DirectHarnessError("protocol", "provider tool call is invalid")
    return {
        "message": {
            "role": message.role,
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ],
            **(
                {"reasoning_content": message.reasoning_content}
                if message.reasoning_content is not None
                else {}
            ),
        }
    }


def _parse_object(raw: str, *, phase: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise DirectHarnessError("protocol", f"{phase} must be JSON text")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_float=_finite_float,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {value}")
            ),
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DirectHarnessError("protocol", f"{phase} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise DirectHarnessError("protocol", f"{phase} must be a JSON object")
    return value


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _unique_pairs(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = member
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _event(sink, kind: str, **fields: Any) -> None:
    sink({"kind": kind, **fields})
