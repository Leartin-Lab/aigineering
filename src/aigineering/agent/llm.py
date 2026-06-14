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

        self.worker_id = worker_id or f"llm:{self._sanitize_model_name()}"
        self._transport = transport

    def _sanitize_model_name(self) -> str:
        """Return a safe model name for use in worker_id slugs."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in self.model)[:64]

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
            payload["tools"] = self._tool_definitions

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self._transport is None:
            raise ValueError(
                "LLMWorker requires api_key, AIGINEERING_API_KEY, or OPENAI_API_KEY"
            )

        url = f"{self.base_url}/chat/completions"
        response = self._call_with_retry(url, headers, payload)
        usage_metadata = _extract_usage(response)

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
            raw_output=_extract_message_content(response),
            parsed_action=None,
            metadata=usage_metadata,
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
        authority/projection boundary.  Only the **first** tool call is
        mapped — each candidate carries a single action.
        """
        if not tool_calls:
            raise ValueError("tool_calls must not be empty")

        first = tool_calls[0]
        func = first.get("function")
        if not isinstance(func, dict):
            raise ValueError("tool call missing 'function' key")
        name = func.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool call missing valid 'name'")
        args_str = func.get("arguments", "{}")
        if isinstance(args_str, str):
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(args_str, dict):
            args = args_str
        else:
            args = {}

        raw_output = json.dumps({"name": name, "args": args}, ensure_ascii=False)
        raw_output = f"/tool {raw_output}"
        parsed_action: dict[str, object] = {
            "type": "tool",
            "payload": {"name": name, "args": args},
        }
        return raw_output, parsed_action


def _extract_message_content(response: Mapping[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response missing choices")

    first = choices[0]
    if not isinstance(first, Mapping):
        raise ValueError("LLM response choice must be an object")

    message = first.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content

    text = first.get("text")
    if isinstance(text, str):
        return text

    raise ValueError("LLM response missing message content")


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


def _extract_usage(response: Mapping[str, object]) -> MappingProxyType | None:
    """Extract token usage metadata from an OpenAI-compatible response."""
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if not isinstance(prompt_tokens, int) and not isinstance(completion_tokens, int):
        return None
    result: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if isinstance(total_tokens, int):
        result["total_tokens"] = total_tokens
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
    parsed: Any = json.loads(decoded)
    if not isinstance(parsed, Mapping):
        raise ValueError("LLM response must be a JSON object")
    return parsed
