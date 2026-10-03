"""OpenAI-compatible LLM worker."""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from aigineering.agent.prompt import contract_prompt, system_prompt
from aigineering.agent.worker import WorkerExecutionError
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.actions import ActionParseError, action_from_dict
from aigineering.protocol.types import Asset, Candidate, Contract
from aigineering.protocol.usage import (
    TOKEN_USAGE_FIELDS,
    normalize_usage_metadata,
)

logger = logging.getLogger(__name__)

# Known LLM provider capabilities.  Providers advertise which features they
# support; the worker uses the capability set to decide what to include in
# API requests and how to interpret responses.
SUPPORTED_CAPABILITIES = frozenset({"tool_calling", "json_schema"})
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024

Transport = Callable[
    [str, Mapping[str, str], Mapping[str, object]],
    Mapping[str, object],
]


@dataclass
class LLMConfig:
    """Configuration for an LLM worker invocation."""

    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    timeout: float = 60.0  # seconds
    max_retries: int = 3
    max_output_tokens: int = 2048
    thinking_mode: str = ""
    retry_backoff: float = 2.0  # multiplier
    capabilities: frozenset[str] = field(default_factory=frozenset)
    tool_definitions: list[dict[str, object]] | None = None
    routing_capabilities: frozenset[str] = field(default_factory=frozenset)
    worker_pools: frozenset[str] = field(default_factory=frozenset)
    profile_id: str = "openai-compatible-v1"
    capacity: int = 1
    registration_version: str = "1"
    response_format: str = ""

    def __post_init__(self) -> None:
        validate_llm_timeout(self.timeout)
        validate_llm_max_retries(self.max_retries)
        validate_llm_retry_backoff(self.retry_backoff)
        validate_llm_max_output_tokens(self.max_output_tokens)
        validate_llm_capacity(self.capacity)
        validate_llm_response_format(self.response_format)


def validate_llm_timeout(value: object) -> float:
    """Validate and preserve a provider request timeout in seconds."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("LLM timeout must be a finite positive number")
    return float(value)


def validate_llm_max_retries(value: object) -> int:
    """Validate a non-negative integer provider retry count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("LLM max_retries must be a non-negative integer")
    return value


def validate_llm_retry_backoff(value: object) -> float:
    """Validate a finite, non-negative retry delay multiplier."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError("retry_backoff must be a finite non-negative number")
    return float(value)


def validate_llm_max_output_tokens(value: object) -> int:
    """Validate the positive integer provider output token limit."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_output_tokens must be an integer")
    if value < 1:
        raise ValueError("max_output_tokens must be at least 1")
    return value


def validate_llm_capacity(value: object) -> int:
    """Validate the positive integer worker capacity."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("capacity must be an integer")
    if value < 1:
        raise ValueError("capacity must be at least 1")
    return value


def validate_llm_response_format(value: object) -> str:
    """Validate the optional provider response format mode."""
    if not isinstance(value, str) or value not in {"", "json_object"}:
        raise ValueError("response_format must be '' or 'json_object'")
    return value


class ProviderError(Exception):
    """Classified provider error with retryability information."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_retryable = status_code in (429, 502, 503, 504) or status_code >= 500


class LLMWorker:
    """Worker that calls an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        worker_id: str | None = None,
        transport: Transport | None = None,
        timeout: float = 60.0,
        config: LLMConfig | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
        capabilities: frozenset[str] | None = None,
        tool_definitions: list[dict[str, object]] | None = None,
        routing_capabilities: frozenset[str] | None = None,
        worker_pools: frozenset[str] | None = None,
        profile_id: str | None = None,
        capacity: int | None = None,
        registration_version: str | None = None,
        max_output_tokens: int | None = None,
        thinking_mode: str | None = None,
        response_format: str | None = None,
    ) -> None:
        if config is not None:
            self.model = config.model
            self.api_key = (
                config.api_key
                or api_key
                or os.environ.get("AIGINEERING_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
            )
            self.base_url = config.base_url.rstrip("/")
            self._timeout = config.timeout
            self._max_retries = config.max_retries
            self._max_output_tokens = (
                max_output_tokens
                if max_output_tokens is not None
                else config.max_output_tokens
            )
            self._thinking_mode = (
                thinking_mode if thinking_mode is not None else config.thinking_mode
            )
            self._retry_backoff = config.retry_backoff
            self._capabilities = (
                capabilities if capabilities is not None else config.capabilities
            )
            self._tool_definitions = (
                tool_definitions
                if tool_definitions is not None
                else config.tool_definitions
            )
            self._routing_capabilities = (
                routing_capabilities
                if routing_capabilities is not None
                else config.routing_capabilities
            )
            self._worker_pools = (
                worker_pools if worker_pools is not None else config.worker_pools
            )
            self.profile_id = (
                profile_id if profile_id is not None else config.profile_id
            )
            self._capacity = capacity if capacity is not None else config.capacity
            self._registration_version = (
                registration_version
                if registration_version is not None
                else config.registration_version
            )
            self._response_format = validate_llm_response_format(
                response_format
                if response_format is not None
                else config.response_format
            )
        else:
            self.model = model
            self.api_key = (
                api_key
                or os.environ.get("AIGINEERING_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
            )
            self.base_url = base_url.rstrip("/")
            self._timeout = timeout
            self._max_retries = max_retries if max_retries is not None else 3
            self._max_output_tokens = (
                max_output_tokens if max_output_tokens is not None else 2048
            )
            self._thinking_mode = thinking_mode or ""
            self._retry_backoff = retry_backoff if retry_backoff is not None else 2.0
            self._capabilities = capabilities or frozenset()
            self._tool_definitions = tool_definitions
            self._routing_capabilities = routing_capabilities or frozenset()
            self._worker_pools = worker_pools or frozenset()
            self.profile_id = profile_id or "openai-compatible-v1"
            self._capacity = capacity if capacity is not None else 1
            self._registration_version = registration_version or "1"
            self._response_format = validate_llm_response_format(
                response_format if response_format is not None else ""
            )

        self._capacity = validate_llm_capacity(self._capacity)
        self._timeout = validate_llm_timeout(self._timeout)
        self._max_retries = validate_llm_max_retries(self._max_retries)
        self._max_output_tokens = validate_llm_max_output_tokens(
            self._max_output_tokens
        )
        self._retry_backoff = validate_llm_retry_backoff(self._retry_backoff)
        if self._thinking_mode not in {"", "enabled", "disabled"}:
            raise ValueError("thinking_mode must be 'enabled', 'disabled', or empty")

        self.worker_id = worker_id or f"llm:{self._sanitize_model_name()}"
        self._transport = transport

    def _sanitize_model_name(self) -> str:
        """Return a safe model name for use in worker_id slugs."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in self.model)[:64]

    def _provider_name(self) -> str:
        """Derive a logical provider name from the base URL."""
        from urllib.parse import urlparse

        parsed = urlparse(self.base_url)
        return parsed.hostname or "unknown"

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate:
        payload: dict[str, object] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": _system_prompt_for_format(self._response_format),
                },
                {
                    "role": "user",
                    "content": contract_prompt(contract, disclosed_assets),
                },
            ],
        }
        if self._response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self._thinking_mode:
            payload["thinking"] = {"type": self._thinking_mode}

        if (
            "tool_calling" in self._capabilities
            and self._tool_definitions is not None
            and len(self._tool_definitions) > 0
        ):
            scoped_tools = _scoped_tool_definitions(
                self._tool_definitions,
                contract.tool_scope,
            )
            if scoped_tools:
                if self._response_format == "json_object":
                    raise WorkerExecutionError(
                        "json_object_with_tools_unsupported",
                        "JSON object response mode cannot be combined with native tools",
                    )
                payload["tools"] = scoped_tools

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self._transport is None:
            raise WorkerExecutionError(
                "missing_api_key",
                "LLMWorker requires api_key, AIGINEERING_API_KEY, or OPENAI_API_KEY",
            )

        url = f"{self.base_url}/chat/completions"
        response = self._call_with_retry(url, headers, payload)
        usage_metadata = _build_usage_metadata(
            response,
            model=self.model,
            provider=self._provider_name(),
            worker_profile=self.profile_id,
        )
        try:
            if _response_was_truncated(response):
                raise WorkerExecutionError(
                    "output_truncated",
                    "LLM response reached the provider output limit",
                )

            tool_calls = _extract_tool_calls(response)
            if tool_calls is not None:
                if self._response_format == "json_object":
                    raise WorkerExecutionError(
                        "json_object_tool_call_response",
                        "JSON object response mode requires one JSON action object",
                    )
                raw_output, parsed_action = self._map_tool_calls_to_actions(tool_calls)
                return Candidate(
                    worker_id=self.worker_id,
                    raw_output=raw_output,
                    parsed_action=parsed_action,
                    metadata=usage_metadata,
                )

            content = _extract_message_content(response)
            if self._response_format == "json_object" and not content.strip():
                raise WorkerExecutionError(
                    "empty_response", "LLM response content is empty"
                )
            normalized_content = (
                _normalize_json_action_wire(content)
                if self._response_format == "json_object"
                else _normalize_provider_action(content)
            )
            if not normalized_content.strip():
                raise WorkerExecutionError(
                    "empty_response", "LLM response content is empty"
                )
            return Candidate(
                worker_id=self.worker_id,
                raw_output=normalized_content,
                parsed_action=None,
                metadata=usage_metadata,
            )
        except WorkerExecutionError as exc:
            # Preserve well-formed reported usage on post-response failures (for
            # example malformed choices or tool arguments).  Runtime owns the
            # durable failure record; this exception only carries the data.
            if exc.usage_metadata is None and usage_metadata is not None:
                exc.usage_metadata = usage_metadata
            raise

    def registration(self) -> WorkerRegistration:
        """Return trusted routing metadata for this stateless LLM worker.

        Registration is control-plane data. Callers persist it through the
        runtime store; it is not disclosed to model prompts.
        """
        return WorkerRegistration(
            worker_id=self.worker_id,
            capabilities=tuple(self._routing_capabilities),
            pools=tuple(self._worker_pools),
            profile_id=self.profile_id,
            capacity=self._capacity,
            version=self._registration_version,
        )

    def _call_with_retry(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._call(url, headers, payload)
            except ProviderError as e:
                last_error = e
                if not e.is_retryable:
                    logger.warning(
                        "LLM call failed with non-retryable error %d: %s",
                        e.status_code,
                        e,
                    )
                    raise
                if attempt >= self._max_retries:
                    logger.error(
                        "LLM call exhausted %d retries on status %d",
                        self._max_retries,
                        e.status_code,
                    )
                    raise
                wait = self._retry_backoff ** (attempt + 1)
                logger.info(
                    "LLM call retry %d/%d after %.0fs (status %d)",
                    attempt + 1,
                    self._max_retries,
                    wait,
                    e.status_code,
                )
                time.sleep(wait)
            except (TimeoutError, OSError) as e:
                last_error = e
                if attempt >= self._max_retries:
                    logger.error(
                        "LLM call exhausted %d retries after timeout/network error",
                        self._max_retries,
                    )
                    raise ProviderError(
                        0,
                        f"Timeout/network error after {self._max_retries} retries: {e}",
                    ) from e
                wait = self._retry_backoff ** (attempt + 1)
                logger.info(
                    "LLM call retry %d/%d after %.0fs (timeout/network)",
                    attempt + 1,
                    self._max_retries,
                    wait,
                )
                time.sleep(wait)
        # Should be unreachable, but guard against edge cases
        raise ProviderError(0, f"LLM call failed: {last_error}") from last_error

    def _call(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        if self._transport is not None:
            return self._transport(url, headers, payload)
        return _post_json(url, headers, payload, timeout=self._timeout)

    @staticmethod
    def _map_tool_calls_to_actions(
        tool_calls: list[dict[str, object]],
    ) -> tuple[str, dict[str, object]]:
        """Map OpenAI *tool_calls* to ``/tool`` method request format.

        OpenAI format (per call)::

            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "search",
                    "arguments": "{\"q\": \"hello\"}"
                }
            }

        Returns a ``(raw_output, parsed_action)`` tuple where *raw_output*
        is the ``/tool`` command string and *parsed_action* is the action
        dictionary that the engine will dispatch through the normal
        authority/projection boundary.

        Multiple calls become one ``/parallel_tool`` method request. Its Plugin
        atomically publishes independently claimable tool tasks plus a Boolean
        join continuation; tools never execute inside the LLM adapter.
        """
        if not tool_calls:
            raise WorkerExecutionError(
                "empty_tool_calls", "tool_calls must not be empty"
            )

        actions: list[dict[str, object]] = []
        for tc in tool_calls:
            func = tc.get("function")
            if not isinstance(func, dict):
                raise WorkerExecutionError(
                    "tool_call_missing_function", "tool call missing 'function' key"
                )
            name = func.get("name")
            if not isinstance(name, str) or not name.strip():
                raise WorkerExecutionError(
                    "tool_call_missing_name", "tool call missing valid 'name'"
                )
            args_str = func.get("arguments", "{}")
            if isinstance(args_str, str):
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError as exc:
                    raise WorkerExecutionError(
                        "tool_call_invalid_arguments",
                        f"tool call {name!r} arguments are not valid JSON",
                    ) from exc
            elif isinstance(args_str, dict):
                args = args_str
            else:
                raise WorkerExecutionError(
                    "tool_call_invalid_arguments",
                    f"tool call {name!r} arguments must be a JSON object",
                )
            if not isinstance(args, dict):
                raise WorkerExecutionError(
                    "tool_call_invalid_arguments",
                    f"tool call {name!r} arguments must decode to a JSON object",
                )
            actions.append({"name": name, "args": args})

        if len(actions) > 1:
            payload = {"calls": actions, "join": "all"}
            return (
                "/parallel_tool "
                + json.dumps(payload, sort_keys=True, ensure_ascii=False),
                {"type": "parallel_tool", "payload": payload},
            )

        raw_output = json.dumps(actions[0], ensure_ascii=False)
        raw_output = f"/tool {raw_output}"
        return raw_output, {"type": "tool", "payload": actions[0]}


def _extract_message_content(response: Mapping[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise WorkerExecutionError(
            "response_missing_choices", "LLM response missing choices"
        )

    first = choices[0]
    if not isinstance(first, Mapping):
        raise WorkerExecutionError(
            "response_choice_not_object", "LLM response choice must be an object"
        )

    message = first.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if "content" in message and content is None:
            raise WorkerExecutionError(
                "empty_response", "LLM response content is empty"
            )
        if isinstance(content, str):
            return content

    text = first.get("text")
    if isinstance(text, str):
        return text

    raise WorkerExecutionError(
        "response_missing_content", "LLM response missing message content"
    )


def _response_was_truncated(response: Mapping[str, object]) -> bool:
    """Return whether the first completion explicitly hit its output limit."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0]
    return isinstance(first, Mapping) and first.get("finish_reason") == "length"


_JSON_ACTION_INSTRUCTIONS = """For this response mode, these instructions replace the slash-action examples above. Return exactly one JSON object and no markdown or prose. Do not return slash commands. For final output use {"type":"exec","outputs":{"report":"first line\\n**second line**","structured":{"items":[1,2]}}}; output strings remain strings, and JSON-valued outputs should be objects or arrays directly, not JSON encoded inside a string. For a planning action use {"type":"plan","payload":{"reason":"..."}}; for a changed invalid assumption use {"type":"replan","payload":{"reason":"..."}}. Other canonical actions use the same type/payload shape. The runtime accepts only supported canonical action types and declared output names."""


def _system_prompt_for_format(response_format: str) -> str:
    if response_format == "json_object":
        return system_prompt() + "\n\n" + _JSON_ACTION_INSTRUCTIONS
    return system_prompt()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _normalize_json_action_wire(content: str) -> str:
    """Validate strict JSON action wire and translate to the canonical parser."""
    try:
        wire = json.loads(
            content,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(wire, Mapping) or not isinstance(wire.get("type"), str):
            raise ValueError("action must be a JSON object with a string type")

        action_type = wire["type"]
        if action_type == "exec":
            if set(wire) != {"type", "outputs"} or not isinstance(
                wire["outputs"], Mapping
            ):
                raise ValueError("exec action requires only an outputs object")
            outputs: dict[str, str] = {}
            for name, value in wire["outputs"].items():
                if isinstance(value, str):
                    outputs[name] = value
                else:
                    outputs[name] = json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
            action = action_from_dict({"type": "exec", "outputs": outputs})
            canonical = {"outputs": dict(action.outputs)}
        else:
            if set(wire) != {"type", "payload"} or not isinstance(
                wire["payload"], Mapping
            ):
                raise ValueError("non-exec action requires only a payload object")
            action = action_from_dict(
                {"type": action_type, "payload": dict(wire["payload"])}
            )
            canonical = dict(action.payload)

        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return f"/{action.type} {encoded}"
    except (
        ActionParseError,
        RecursionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise WorkerExecutionError(
            "malformed_json_action",
            "LLM response was not one valid canonical JSON action",
        ) from exc


def _normalize_provider_action(raw_output: str) -> str:
    """Normalize provider presentation quirks without relaxing the protocol.

    Some reasoning models expose a leading ``<think>`` block, and some models
    encode JSON-valued Asset content as an object instead of the required JSON
    string.  Both are provider presentation details.  The canonical action
    parser still receives exactly one slash action with string output values.
    """
    action = raw_output.strip()
    if action.startswith("<think>"):
        marker = "</think>"
        end = action.find(marker)
        if end < 0:
            return action
        action = action[end + len(marker) :].strip()

    fenced = action.startswith("```") and action.endswith("```")
    candidate = action
    if fenced:
        lines = action.splitlines()
        if len(lines) < 3:
            return action
        candidate = "\n".join(lines[1:-1]).strip()

    command, separator, body = candidate.partition(" ")
    if command != "/exec" or not separator or not body.strip():
        return action
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return action
    if not isinstance(payload, Mapping):
        return action
    raw_outputs = payload.get("outputs", payload)
    if not isinstance(raw_outputs, Mapping):
        return action

    outputs: dict[str, str] = {}
    for name, content in raw_outputs.items():
        if not isinstance(name, str):
            return action
        if isinstance(content, str):
            outputs[name] = content
        elif isinstance(content, (Mapping, list)):
            outputs[name] = json.dumps(
                content,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            return action
    return "/exec " + json.dumps(
        {"outputs": outputs},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _extract_tool_calls(
    response: Mapping[str, object],
) -> list[dict[str, object]] | None:
    """Extract tool_calls from an OpenAI-compatible chat completion response.

    Returns ``None`` when the message has no tool_calls (text-only response).
    """
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first = choices[0]
    if not isinstance(first, Mapping):
        return None

    message = first.get("message")
    if not isinstance(message, Mapping):
        return None

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and len(tool_calls) > 0:
        return tool_calls

    return None


def _scoped_tool_definitions(
    definitions: list[dict[str, object]],
    tool_scope: tuple[str, ...],
) -> list[dict[str, object]]:
    """Return only well-formed provider tools declared by this Contract."""
    allowed = set(tool_scope)
    scoped: list[dict[str, object]] = []
    seen: set[str] = set()
    for definition in definitions:
        function = definition.get("function")
        if not isinstance(function, Mapping):
            raise WorkerExecutionError(
                "invalid_tool_definition",
                "tool definition requires an object function",
            )
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise WorkerExecutionError(
                "invalid_tool_definition",
                "tool definition requires a non-empty function.name",
            )
        if name in seen:
            raise WorkerExecutionError(
                "duplicate_tool_definition",
                f"tool definition {name!r} is duplicated",
            )
        seen.add(name)
        if name in allowed:
            scoped.append(definition)
    return scoped


def _extract_usage(response: Mapping[str, object]) -> MappingProxyType | None:
    """Extract token usage metadata from an OpenAI-compatible response."""
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    result = {
        token_field: usage[token_field]
        for token_field in TOKEN_USAGE_FIELDS
        if token_field in usage
    }
    normalized = normalize_usage_metadata(result)
    return MappingProxyType(normalized) if normalized else None


def _build_usage_metadata(
    response: Mapping[str, object],
    *,
    model: str,
    provider: str,
    worker_profile: str,
) -> MappingProxyType | None:
    """Build usage metadata with token counts, model, and provider.

    API keys are never included in the returned metadata.
    """
    result = dict(_extract_usage(response) or {})
    result["model"] = model
    result["provider"] = provider
    result["worker_profile"] = worker_profile
    return MappingProxyType(normalize_usage_metadata(result) or {})


def _post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
    timeout: float,
) -> Mapping[str, object]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw_response = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ProviderError(e.code, f"HTTP {e.code}: {e.reason}") from e
    if len(raw_response) > MAX_PROVIDER_RESPONSE_BYTES:
        raise WorkerExecutionError(
            "response_too_large", "LLM response exceeds the maximum size"
        )
    try:
        decoded = raw_response.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerExecutionError(
            "response_invalid_encoding", "LLM response is not valid UTF-8"
        ) from exc
    try:
        parsed: Any = json.loads(decoded)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise WorkerExecutionError(
            "response_invalid_json", "LLM response is not valid JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise WorkerExecutionError(
            "response_not_object", "LLM response must be a JSON object"
        )
    return parsed
