"""Tool executor — executes tool calls through ToolRegistry, returning Candidate results (not a Worker — see ADR-006, v0.3.6)."""

from __future__ import annotations

import json
from typing import Any

from aigineering.core.tools import ToolRegistry
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
        try:
            result = self._registry.run(tool_name, args)
            if not isinstance(result, str):
                raise TypeError("tool result must be a string")
            ok = True
            error = ""
            error_type = ""
        except Exception as e:
            result = ""
            ok = False
            error = str(e)
            error_type = type(e).__name__

        obs = json.dumps(
            {
                "ok": ok,
                "tool": tool_name,
                "result": result,
                "error": error,
                "error_type": error_type,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return Candidate(
            worker_id=f"tool_worker:{tool_name}",
            raw_output=obs,
        )
