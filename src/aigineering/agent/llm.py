"""OpenAI-compatible LLM worker."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from aigineering.agent.prompt import contract_prompt, system_prompt
from aigineering.protocol.types import Asset, Candidate, Contract

logger = logging.getLogger(__name__)

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

        self.worker_id = worker_id or f"llm:{self.model}"
        self._transport = transport

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
                        0, f"Timeout/network error after {self._max_retries} retries: {e}"
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
    result: dict[str, object] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
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
