"""MCPWorker — boundary wrapper that exposes MCP execution through the Worker protocol (v0.5.0).

Wraps MCP server backends as a proper Worker protocol implementation. MCP
execution follows the same candidate → projection → authority → trace
boundary as LLM and mock workers.  Produces ``_mcp_obs_*`` observation
assets (observations, not outputs — see ADR-006).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.agent.mcp_executor import MCPExecutor
from aigineering.core.methods import method_payload

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, Candidate, Contract


class MCPWorker:
    """Worker that executes MCP tool calls.

    MCPWorker is a boundary wrapper: it presents MCP tool execution as a
    standard Worker protocol implementation.  The low-level MCPExecutor
    remains an internal adapter.  MCPWorker never commits directly —
    it returns a Candidate that must pass through projection.
    """

    worker_id: str

    def __init__(
        self,
        mcp_servers: dict[str, object] | None = None,
        worker_id: str = "mcp_worker",
    ) -> None:
        self._executor = MCPExecutor(mcp_servers)
        self.worker_id = worker_id

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate:
        """Execute an MCP tool request from *contract* and return a Candidate.

        Parses the MCP tool name from the contract's method payload (stripping
        the ``"mcp:"`` prefix if present), executes through the MCP executor,
        and returns a Candidate whose raw_output is a JSON observation object.
        """
        payload = method_payload(contract)
        tool_payload = (
            payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
        )
        tool_name = tool_payload.get("name", "")
        args = tool_payload.get("args", {})

        if isinstance(tool_name, str) and tool_name.startswith("mcp:"):
            tool_name = tool_name[4:]

        return self._executor.invoke(
            tool_name,
            args if isinstance(args, dict) else {},
            contract.id,
        )
