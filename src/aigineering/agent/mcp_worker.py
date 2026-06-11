"""MCP worker — executes MCP tool calls, returning Candidate results (v0.4.2)."""

from __future__ import annotations

import json
from typing import Any

from aigineering.protocol.types import Candidate


class MCPWorker:
    """Worker that executes MCP tool calls, returning Candidate results.

    MCPWorker splits MCP tool execution from the MCP method lifecycle:
    the handler owns validation (tool_name, tool_scope, server existence)
    and delegates execution to MCPWorker, which returns a Candidate.
    The Candidate must go through projection before becoming a runtime fact.

    Each server in ``mcp_servers`` is a callable with signature
    ``(tool_name: str, args: dict) -> str``.  The tool name is MCP-style
    with a server prefix (e.g. ``"search.query"`` → server ``"search"``).
    """

    def __init__(self, mcp_servers: dict[str, Any] | None = None) -> None:
        self._servers: dict[str, Any] = mcp_servers or {}

    def invoke(self, tool_name: str, args: dict[str, Any], contract_id: str) -> Candidate:
        """Execute an MCP tool call and return a Candidate with the result.

        Args:
            tool_name: MCP tool name with server prefix (e.g. ``"search.query"``).
            args: Arguments to pass to the MCP tool handler.
            contract_id: ID of the contract requesting the tool (for provenance).

        Returns:
            Candidate whose ``raw_output`` is a JSON object with fields
            ``ok``, ``tool``, ``result``, and ``error``.
        """
        try:
            server_name = tool_name.split(".", 1)[0]
            server = self._servers[server_name]
            result = server(tool_name, args)
            ok = True
            error = ""
        except KeyError:
            result = ""
            ok = False
            error = f"unknown mcp server for tool '{tool_name}'"
        except Exception as e:
            result = ""
            ok = False
            error = str(e)

        obs = json.dumps(
            {"ok": ok, "tool": tool_name, "result": result, "error": error},
            sort_keys=True,
            ensure_ascii=False,
        )
        return Candidate(
            worker_id=f"mcp_worker:{tool_name}",
            raw_output=obs,
        )
