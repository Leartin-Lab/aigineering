"""OpenAI-compatible LLM worker."""

from __future__ import annotations

import json
import logging
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
from aigineering.protocol.types import Asset, Candidate, Contract

logger = logging.getLogger(__name__)

# Known LLM provider capabilities.  Providers advertise which features they
# support; the worker uses the capability set to decide what to include in
# API requests and how to interpret responses.
SUPPORTED_CAPABILITIES = frozenset({"tool_calling", "json_schema"})

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
    retry_backoff: float = 2.0  # multiplier
    capabilities: frozenset[str] = field(default_factory=frozenset)
    tool_definitions: list[dict[str, object]] | None = None
    routing_capabilities: frozenset[str] = field(default_factory=frozenset)
    worker_pools: frozenset[str] = field(default_factory=frozenset)
    profile_id: str = "openai-compatible-v1"
    capacity: int = 1


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
        timeout: int = 60,
        config: LLMConfig | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
        capabilities: frozenset[str] | None = None,
        tool_definitions: list[dict[str, object]] | None = None,
        routing_capabilities: frozenset[str] | None = None,
        worker_pools: frozenset[str] | None = None,
        profile_id: str | None = None,
        capacity: int | None = None,
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
            self._retry_backoff = retry_backoff if retry_backoff is not None else 2.0
            self._capabilities = capabilities or frozenset()
            self._tool_definitions = tool_definitions
            self._routing_capabilities = routing_capabilities or frozenset()
            self._worker_pools = worker_pools or frozenset()
            self.profile_id = profile_id or "openai-compatible-v1"
            self._capacity = capacity if capacity is not None else 1

        if self._capacity < 1:
            raise ValueError("capacity must be at least 1")

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
            "messages": [
                {"role": "system", "content": system_prompt()},
                {
                    "role": "user",
                    "content": contract_prompt(contract, disclosed_assets),
                },
            ],
        }

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

        tool_calls = _extract_tool_calls(response)
        if tool_calls is not None:
            raw_output, parsed_action = self._map_tool_calls_to_actions(tool_calls)
            return Candidate(
                worker_id=self.worker_id,
                raw_output=raw_output,
                parsed_action=parsed_action,
                metadata=usage_metadata,
            )

        return Candidate(
            worker_id=self.worker_id,
            raw_output=_normalize_provider_action(_extract_message_content(response)),
            parsed_action=None,
            metadata=usage_metadata,
        )

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

        Multiple calls are rejected visibly. The runtime has no multi-action
        commitment primitive, so selecting only one would silently discard
        proposed work and emitting an unsupported envelope would fail later at
        Candidate encoding.
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
            payload = {
                "reason": (
                    "provider proposed multiple tool calls; publish each call as "
                    "independently claimable work or retry with exactly one call"
                ),
                "proposed_tools": [str(action["name"]) for action in actions],
            }
            return (
                "/fail " + json.dumps(payload, sort_keys=True, ensure_ascii=False),
                {"type": "fail", "payload": payload},
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
        if isinstance(content, str):
            return content

    text = first.get("text")
    if isinstance(text, str):
        return text

    raise WorkerExecutionError(
        "response_missing_content", "LLM response missing message content"
    )


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
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if type(prompt_tokens) is not int and type(completion_tokens) is not int:
        return None
    result: dict[str, object] = {}
    if type(prompt_tokens) is int:
        result["prompt_tokens"] = prompt_tokens
    if type(completion_tokens) is int:
        result["completion_tokens"] = completion_tokens
    if type(total_tokens) is int:
        result["total_tokens"] = total_tokens
    return MappingProxyType(result)


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
    usage = _extract_usage(response)
    if usage is None:
        result: dict[str, object] = {
            "model": model,
            "provider": provider,
            "worker_profile": worker_profile,
        }
        return MappingProxyType(result)

    result = dict(usage)
    result["model"] = model
    result["provider"] = provider
    result["worker_profile"] = worker_profile
    return MappingProxyType(result)


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
            decoded = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise ProviderError(e.code, f"HTTP {e.code}: {e.reason}") from e
    try:
        parsed: Any = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise WorkerExecutionError(
            "response_invalid_json", "LLM response is not valid JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise WorkerExecutionError(
            "response_not_object", "LLM response must be a JSON object"
        )
    return parsed
