"""Strict raw-wire adversaries and fake time for the direct-loop tests."""

from __future__ import annotations


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RawResponseClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = []
        self.timeouts = []

    def complete(self, *, messages, tools, timeout_seconds) -> str:
        del tools
        self.calls.append([dict(item) for item in messages])
        self.timeouts.append(timeout_seconds)
        if not self.responses:
            raise AssertionError("raw response fixture is exhausted")
        return self.responses.pop(0)


class ScriptedResponse:
    """Small response builder for malformed but deterministic wire cases."""

    @staticmethod
    def tool_call(name: str, arguments: str, call_id: str = "tool-1") -> str:
        import json

        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            ],
        }
        return json.dumps({"message": message, "usage": None}, separators=(",", ":"))
