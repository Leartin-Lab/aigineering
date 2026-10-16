"""MCP executor — executes MCP tool calls, returning Candidate results (not a Worker — see ADR-006, v0.4.2)."""

from __future__ import annotations

import json
from typing import Any

from aigineering.agent.worker import WorkerExecutionError
from aigineering.protocol.types import Candidate


class MCPExecutor:
    """Executes MCP tool calls, returning Candidate results.

    MCPExecutor (not a Worker — see ADR-006) splits MCP tool execution
    from the MCP method lifecycle: the handler owns validation (tool_name,
    tool_scope, server existence) and delegates execution to MCPExecutor,
    which returns a Candidate. The Candidate must go through projection
    before becoming a runtime fact.

    Each server in ``mcp_servers`` is a callable with signature
    ``(tool_name: str, args: dict) -> str``.  The tool name is MCP-style
    with a server prefix (e.g. ``"search.query"`` → server ``"search"``).
    """

    def __init__(self, mcp_servers: dict[str, Any] | None = None) -> None:
        self._servers: dict[str, Any] = mcp_servers or {}

    def invoke(
        self, tool_name: str, args: dict[str, Any], contract_id: str
    ) -> Candidate:
        """Execute an MCP tool call and return a Candidate with the result.

        Args:
            tool_name: MCP tool name with server prefix (e.g. ``"search.query"``).
            args: Arguments to pass to the MCP tool handler.
            contract_id: ID of the contract requesting the tool (for provenance).

        Returns:
            Candidate whose ``raw_output`` is a JSON object with fields
            ``ok``, ``tool``, ``result``, and ``error``.
        """
        if not isinstance(tool_name, str) or not tool_name:
            raise WorkerExecutionError(
                "mcp_name_invalid", "MCP tool name must be nonempty."
            )
        if not isinstance(args, dict):
            raise WorkerExecutionError(
                "mcp_arguments_invalid", "MCP arguments must be an object."
            )
        server_name = tool_name.split(".", 1)[0]
        server = self._servers.get(server_name)
        if not callable(server):
            raise WorkerExecutionError(
                "mcp_server_unavailable", "MCP server is unavailable."
            )
        try:
            result = server(tool_name, args)
        except Exception as e:
            raise WorkerExecutionError("mcp_call_failed", "MCP call failed.") from e
        if not isinstance(result, str):
            raise WorkerExecutionError(
                "mcp_result_invalid", "MCP server returned an invalid result."
            )

        obs = json.dumps(
            {
                "ok": True,
                "tool": tool_name,
                "result": result,
                "error": "",
                "error_type": "",
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return Candidate(
            worker_id=f"mcp_worker:{tool_name}",
            raw_output=obs,
        )
