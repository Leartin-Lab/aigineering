"""MCPWorker — boundary wrapper that exposes MCP execution through the Worker protocol (v0.5.0).

Wraps MCP server backends as a proper Worker protocol implementation. MCP
execution follows the same candidate → projection → authority → trace
boundary as LLM and mock workers.  Produces ``_mcp_obs_*`` observation
assets (observations, not outputs — see ADR-006).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.agent.mcp_executor import MCPExecutor
from aigineering.core.capability_descriptors import verify_descriptor
from aigineering.core.methods import method_payload
from aigineering.protocol.types import Candidate

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, Contract


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
            payload.get("payload", {})
            if isinstance(payload.get("payload"), dict)
            else {}
        )
        tool_name = tool_payload.get("name", "")
        args = tool_payload.get("args", {})

        scoped_name = tool_name
        if isinstance(tool_name, str) and tool_name.startswith("mcp:"):
            tool_name = tool_name[4:]
        server_name = tool_name.split(".", 1)[0] if isinstance(tool_name, str) else ""
        descriptor_name = f"_mcp_{server_name}"
        descriptor = next(
            (asset for asset in disclosed_assets if asset.name == descriptor_name),
            None,
        )
        if scoped_name not in contract.tool_scope:
            obs = json.dumps(
                {
                    "ok": False,
                    "tool": str(tool_name),
                    "result": "",
                    "error": f"tool '{scoped_name}' is not in contract.tool_scope",
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        elif descriptor is None or not verify_descriptor(descriptor, kind="mcp"):
            obs = json.dumps(
                {
                    "ok": False,
                    "tool": str(tool_name),
                    "result": "",
                    "error": f"MCP descriptor '{descriptor_name}' is missing or invalid",
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        else:
            obs = self._executor.invoke(
                tool_name,
                args if isinstance(args, dict) else {},
                contract.id,
            ).raw_output
        if len(contract.outputs) != 1:
            return Candidate(
                worker_id=self.worker_id,
                raw_output="MCP method contract must declare exactly one observation output",
            )
        outputs = {contract.outputs[0]: obs}
        return Candidate(
            worker_id=self.worker_id,
            raw_output=json.dumps(
                {"type": "exec", "outputs": outputs},
                sort_keys=True,
                ensure_ascii=False,
            ),
            parsed_action={"type": "exec", "outputs": outputs},
        )
