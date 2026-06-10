"""OpenAI-compatible LLM worker."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib import request

from aigineering.protocol.types import Asset, Candidate, Contract

Transport = Callable[
    [str, Mapping[str, str], Mapping[str, object]],
    Mapping[str, object],
]


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
    ) -> None:
        self.model = model
        self.api_key = (
            api_key
            or os.environ.get("AIGINEERING_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id or f"llm:{model}"
        self._transport = transport
        self._timeout = timeout

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate:
        payload: dict[str, object] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": _contract_prompt(contract, disclosed_assets),
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

        response = self._call(
            f"{self.base_url}/chat/completions",
            headers,
            payload,
        )
        return Candidate(
            worker_id=self.worker_id,
            raw_output=_extract_message_content(response),
            parsed_action=None,
        )

    def _call(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        if self._transport is not None:
            return self._transport(url, headers, payload)
        return _post_json(url, headers, payload, timeout=self._timeout)


def _system_prompt() -> str:
    return (
        "You are an Aigineering worker. Your output is only a candidate, "
        "not committed state. Return only asset lines in the exact format "
        "`asset_name: content`. Use only declared output names. Do not add "
        "markdown, explanations, or undeclared assets."
    )


def _contract_prompt(contract: Contract, assets: list[Asset]) -> str:
    lines = [
        f"Contract name: {contract.name}",
        f"Description: {contract.description}",
        "Declared inputs: " + ", ".join(contract.inputs),
        "Declared outputs: " + ", ".join(contract.outputs),
        "",
        "Disclosed assets:",
    ]
    for asset in assets:
        lines.append(f"- {asset.name}: {asset.content}")
    return "\n".join(lines)


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


def _post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
    timeout: int,
) -> Mapping[str, object]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=dict(headers), method="POST")
    with request.urlopen(req, timeout=timeout) as response:  # noqa: S310
        decoded = response.read().decode("utf-8")
    parsed: Any = json.loads(decoded)
    if not isinstance(parsed, Mapping):
        raise ValueError("LLM response must be a JSON object")
    return parsed
