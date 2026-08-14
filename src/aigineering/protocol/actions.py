"""Structured worker action parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aigineering.protocol.immutability import deep_freeze

if TYPE_CHECKING:
    from aigineering.protocol.types import Candidate

_SUPPORTED_ACTIONS = {
    "exec",
    "plan",
    "replan",
    "tool",
    "parallel_tool",
    "retry",
    "fail",
}


class ActionParseError(ValueError):
    """Raised when a structured worker action cannot be parsed."""


@dataclass(frozen=True)
class WorkerAction:
    """Parsed worker action."""

    type: str
    outputs: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", deep_freeze(self.outputs))
        object.__setattr__(self, "payload", deep_freeze(self.payload))


def parse_action(raw_output: str) -> WorkerAction:
    """Parse one of the worker actions supported by the wire protocol."""

    stripped = _strip_fence(raw_output.strip())
    if not stripped.startswith("/"):
        raise ActionParseError("worker action must start with '/'")

    command, _, body = stripped.partition(" ")
    action_type = command[1:]
    if action_type not in _SUPPORTED_ACTIONS:
        raise ActionParseError(f"unsupported worker action '/{action_type}'")

    payload = _parse_payload(body.strip())
    if action_type == "exec":
        return WorkerAction(type=action_type, outputs=_parse_exec_outputs(payload))
    return WorkerAction(type=action_type, payload=dict(payload))


def action_from_dict(data: Mapping[str, Any]) -> WorkerAction:
    """Validate and convert a pre-parsed action dictionary."""

    action_type = data.get("type")
    if not isinstance(action_type, str):
        raise ActionParseError("worker action type must be a string")
    if action_type not in _SUPPORTED_ACTIONS:
        raise ActionParseError(f"unsupported worker action '/{action_type}'")

    if action_type == "exec":
        raw_outputs = data.get("outputs", {})
        if not isinstance(raw_outputs, Mapping):
            raise ActionParseError("/exec outputs must be a JSON object")
        return WorkerAction(type=action_type, outputs=_parse_exec_outputs(raw_outputs))

    payload = data.get("payload", {})
    if not isinstance(payload, Mapping):
        raise ActionParseError(f"/{action_type} payload must be a JSON object")
    return WorkerAction(type=action_type, payload=dict(payload))


def parse_method_action(candidate: "Candidate") -> WorkerAction | None:
    """Parse a method action from a candidate's parsed_action or raw_output.

    Returns **None** if the candidate does not contain a valid method action
    (plan, replan, tool, retry, fail).
    """
    parsed = candidate.parsed_action
    if isinstance(parsed, Mapping) and parsed.get("type") in {
        "plan",
        "replan",
        "tool",
        "parallel_tool",
        "retry",
        "fail",
    }:
        try:
            return action_from_dict(parsed)
        except ActionParseError:
            return None

    if not candidate.raw_output.strip().startswith("/"):
        return None
    try:
        action = parse_action(candidate.raw_output)
    except (ActionParseError, json.JSONDecodeError):
        return None
    if action.type in {"plan", "replan", "tool", "parallel_tool", "retry", "fail"}:
        return action
    return None


def _parse_payload(body: str) -> Mapping[str, Any]:
    if not body:
        return {}
    parsed = json.loads(body)
    if not isinstance(parsed, Mapping):
        raise ActionParseError("worker action payload must be a JSON object")
    return parsed


def _parse_exec_outputs(payload: Mapping[str, Any]) -> dict[str, str]:
    raw_outputs = payload.get("outputs", payload)
    if not isinstance(raw_outputs, Mapping):
        raise ActionParseError("/exec outputs must be a JSON object")

    outputs: dict[str, str] = {}
    for name, content in raw_outputs.items():
        if not isinstance(name, str) or not name.strip():
            raise ActionParseError("/exec output names must be non-empty strings")
        if not isinstance(content, str) or not content.strip():
            raise ActionParseError("/exec output content must be non-empty strings")
        outputs[name.strip()] = content.strip()
    return outputs


def _strip_fence(raw: str) -> str:
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return raw
