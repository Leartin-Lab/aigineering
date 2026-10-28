"""Tool executor — executes tool calls through ToolRegistry, returning Candidate results (not a Worker — see ADR-006, v0.3.6)."""

from __future__ import annotations

import json
import time
from typing import Any

from aigineering.agent.worker import WorkerExecutionError
from aigineering.core.tools import ToolRegistry
from aigineering.core.tool_schema import ToolOutputLimitError, ToolSchemaValidationError
from aigineering.protocol.types import Candidate


class ToolExecutor:
    """Executes tool calls through ToolRegistry, returning Candidate results.

    ToolExecutor (not a Worker — see ADR-006) splits tool execution from
    the tool method lifecycle: the handler owns validation (tool_name,
    tool_scope, registry existence) and delegates execution to ToolExecutor,
    which returns a Candidate. The Candidate must go through projection
    before becoming a runtime fact.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def invoke(
        self, tool_name: str, args: dict[str, Any], contract_id: str
    ) -> Candidate:
        """Execute a tool call and return a Candidate with the result.

        Args:
            tool_name: Name of the tool to invoke.
            args: Arguments to pass to the tool handler.
            contract_id: ID of the contract requesting the tool (for provenance).

        Returns:
            Candidate whose ``raw_output`` is a JSON object with fields
            ``ok``, ``tool``, ``result``, and ``error``.
        """
        if not isinstance(tool_name, str) or not tool_name:
            raise WorkerExecutionError(
                "tool_action_invalid", "Tool name must be nonempty."
            )
        if not isinstance(args, dict):
            raise WorkerExecutionError(
                "tool_arguments_invalid", "Tool arguments must be an object."
            )
        spec = self._registry.get_spec(tool_name)
        if spec is None:
            raise WorkerExecutionError("tool_unavailable", "Tool is unavailable.")
        started_ns = time.perf_counter_ns()
        try:
            result = self._registry.run(tool_name, args)
            if not isinstance(result, str):
                raise TypeError("tool result must be a string")
            result_bytes = len(result.encode("utf-8"))
        except Exception as e:
            if isinstance(e, TimeoutError):
                code, message = "tool_timeout", "Tool execution timed out."
            elif isinstance(e, ToolSchemaValidationError):
                code, message = (
                    "tool_schema_invalid",
                    "Tool input or output is invalid.",
                )
            elif isinstance(e, ToolOutputLimitError):
                code, message = (
                    "tool_result_too_large",
                    "Tool result exceeds its limit.",
                )
            else:
                code, message = "tool_execution_failed", "Tool execution failed."
            raise WorkerExecutionError(code, message) from e

        obs = json.dumps(
            {
                "ok": True,
                "tool": tool_name,
                "result": result,
                "error": "",
                "error_type": "",
                "retryable": False,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return Candidate(
            worker_id=f"tool_worker:{tool_name}",
            raw_output=obs,
            metadata={
                "contract_id": contract_id,
                "tool": tool_name,
                "tool_version": spec.version,
                "duration_ms": (time.perf_counter_ns() - started_ns) // 1_000_000,
                "result_bytes": result_bytes,
                "error_type": "",
                "retryable": False,
            },
        )
