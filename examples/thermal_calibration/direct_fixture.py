"""Explicit scripted client and transient-fault wrapper for offline F1b runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aigineering.core.tools import ToolRegistry
from examples.thermal_calibration.direct_harness import DirectTransientToolError
from examples.thermal_calibration.tools import build_registry

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval"


def fixture_case(case_id: str) -> dict[str, Any]:
    manifest = json.loads((_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    selected = [row for row in manifest["cases"] if row["case_id"] == case_id]
    if len(selected) != 1:
        raise ValueError("unknown or ambiguous evaluation case")
    return json.loads(
        (_FIXTURES / selected[0]["case_file"]).read_text(encoding="utf-8")
    )


def fixture_standard(case_id: str) -> dict[str, Any]:
    manifest = json.loads((_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    selected = [row for row in manifest["cases"] if row["case_id"] == case_id]
    if len(selected) != 1:
        raise ValueError("unknown or ambiguous evaluation case")
    return json.loads(
        (_FIXTURES / selected[0]["standard_file"]).read_text(encoding="utf-8")
    )


def build_fault_registry(*, fail_first_normalize: bool = False) -> ToolRegistry:
    """Build the frozen registry with one explicit, test-only transient fault."""
    registry = build_registry()
    original_run = registry.run
    remaining = [fail_first_normalize]

    def run(name: str, args: dict[str, Any]) -> str:
        if name == "normalize_calibration" and remaining[0]:
            remaining[0] = False
            raise DirectTransientToolError("fixture transient failure")
        return original_run(name, args)

    registry.run = run
    return registry


class ScriptedCalibrationClient:
    """Deterministic protocol participant; never calls a model provider."""

    def __init__(
        self,
        case: dict[str, Any],
        standard: dict[str, Any],
        *,
        retry_transient: bool = False,
        request_extra_tool: bool = False,
        repeat_tool_id: bool = False,
        parallel_calls: bool = False,
        clock=None,
        advance_on_call: float = 0.0,
    ) -> None:
        self.case = case
        self.standard = standard
        self.retry_transient = retry_transient
        self.request_extra_tool = request_extra_tool
        self.repeat_tool_id = repeat_tool_id
        self.parallel_calls = parallel_calls
        self.clock = clock
        self.advance_on_call = advance_on_call
        self.calls: list[dict[str, Any]] = []
        self.requested_tools: list[str] = []
        self.requested_arguments: list[dict[str, Any]] = []
        self.requested_call_ids: list[str] = []
        self.timeouts: list[float] = []
        self.report_json: str | None = None
        self._stage = "normalize"
        self._saw_transient = False
        self._emitted_extra_tool = False

    def complete(self, *, messages, tools, timeout_seconds) -> str:
        del tools
        self.calls.append({"messages": [dict(item) for item in messages]})
        self.timeouts.append(timeout_seconds)
        if self.clock is not None and self.advance_on_call:
            self.clock.advance(self.advance_on_call)

        if self._stage == "normalize":
            last_tool = _last_tool_message(messages)
            if last_tool is not None:
                payload = _strict_loads(last_tool["content"])
                if payload["error_code"] == "transient_tool_failure":
                    self._saw_transient = True
                    if not self.retry_transient:
                        return _final("{}")
                elif payload["ok"] is True:
                    self._stage = "analyze"
            if self._stage == "normalize":
                return self._tool_call(
                    "normalize_calibration",
                    {"case": self.case, "standard": self.standard},
                    "tool-retry" if self._saw_transient else "tool-1",
                )

        if self._stage == "analyze":
            last_tool = _last_tool_message(messages)
            if last_tool is not None:
                payload = _strict_loads(last_tool["content"])
                if payload["ok"] is True:
                    tool_result = _strict_loads(payload["result"])
                    if tool_result.get("schema") == "thermal-calibration-report-v1":
                        self.report_json = payload["result"]
                        if self.request_extra_tool and not self._emitted_extra_tool:
                            self._emitted_extra_tool = True
                            return self._tool_call(
                                "normalize_calibration",
                                {"case": self.case, "standard": self.standard},
                                "tool-extra",
                            )
                        return _final(self.report_json)
                    return self._tool_call(
                        "analyze_calibration", {"normalized": tool_result}, "tool-2"
                    )
                if (
                    payload["error_code"] == "transient_tool_failure"
                    and self.retry_transient
                ):
                    return self._tool_call(
                        "normalize_calibration",
                        {"case": self.case, "standard": self.standard},
                        "tool-retry",
                    )
            return self._tool_call(
                "normalize_calibration",
                {"case": self.case, "standard": self.standard},
                "tool-1",
            )

        raise AssertionError("unknown scripted state")

    def _tool_call(self, name, arguments, tool_id) -> str:
        self.requested_tools.append(name)
        self.requested_arguments.append(arguments)
        if self.repeat_tool_id and self.calls and tool_id == "tool-2":
            tool_id = "tool-1"
        self.requested_call_ids.append(tool_id)
        calls = [
            {
                "id": tool_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        arguments, sort_keys=True, separators=(",", ":")
                    ),
                },
            }
        ]
        if self.parallel_calls:
            calls.append(
                {
                    "id": "tool-parallel",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, sort_keys=True),
                    },
                }
            )
        return _wire({"role": "assistant", "content": None, "tool_calls": calls})


def _last_tool_message(messages):
    for message in reversed(messages):
        if message.get("role") == "tool":
            return message
    return None


def _strict_loads(value):
    return json.loads(
        value,
        object_pairs_hook=_unique_pairs,
        parse_constant=lambda item: (_ for _ in ()).throw(
            ValueError(f"invalid JSON constant {item}")
        ),
    )


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _wire(message):
    return json.dumps({"message": message, "usage": None}, separators=(",", ":"))


def _final(report):
    return _wire({"role": "assistant", "content": report, "tool_calls": []})
