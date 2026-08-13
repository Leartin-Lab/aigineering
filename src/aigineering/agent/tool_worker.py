"""ToolWorker — boundary wrapper that exposes tool execution through the Worker protocol (v0.5.0).

Wraps ToolRegistry as a proper Worker protocol implementation. Tool execution
follows the same candidate → projection → authority → trace boundary as LLM
and mock workers.  Produces ``_tool_obs_*`` observation assets (observations,
not outputs — see ADR-006).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.agent.tool_executor import ToolExecutor
from aigineering.core.capability_descriptors import verify_descriptor
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.types import Candidate

if TYPE_CHECKING:
    from aigineering.core.tools import ToolRegistry
    from aigineering.protocol.types import Asset, Contract


class ToolWorker:
    """Worker that executes tool calls through ToolRegistry.

    ToolWorker is a boundary wrapper: it presents tool execution as a
    standard Worker protocol implementation.  The low-level ToolExecutor
    remains an internal adapter.  ToolWorker never commits directly —
    it returns a Candidate that must pass through projection.
    """

    worker_id: str

    def __init__(
        self,
        registry: ToolRegistry,
        worker_id: str = "tool_worker",
    ) -> None:
        self._registry = registry
        self._executor = ToolExecutor(registry)
        self.worker_id = worker_id

    def registration(self) -> WorkerRegistration:
        """Route this Worker only to ordinary tool-execution Contracts."""
        return WorkerRegistration(
            self.worker_id,
            capabilities=("tool-execution",),
            profile_id="tool-worker-v1",
        )

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate:
        """Execute a tool request from *contract* and return a Candidate.

        Parses the tool name and arguments from the contract's method
        payload, executes through ToolRegistry, and returns a Candidate
        whose raw_output is a JSON observation object with fields
        ``ok``, ``tool``, ``result``, and ``error``.
        """
        payload = method_payload(contract)
        tool_payload = (
            payload.get("payload", {})
            if isinstance(payload.get("payload"), dict)
            else {}
        )
        tool_name = tool_payload.get("name", "")
        args = tool_payload.get("args", {})

        if not isinstance(tool_name, str) or not tool_name:
            obs = json.dumps(
                {
                    "ok": False,
                    "tool": str(tool_name),
                    "result": "",
                    "error": "tool action missing string payload.name",
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            return Candidate(
                worker_id=self.worker_id,
                raw_output=obs,
            )

        descriptor_name = f"_tool_capability_{tool_name}"
        descriptor = next(
            (asset for asset in disclosed_assets if asset.name == descriptor_name),
            None,
        )
        if tool_name not in contract.tool_scope:
            obs = json.dumps(
                {
                    "ok": False,
                    "tool": tool_name,
                    "result": "",
                    "error": f"tool '{tool_name}' is not in contract.tool_scope",
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        elif descriptor is None or not verify_descriptor(descriptor, kind="tool"):
            obs = json.dumps(
                {
                    "ok": False,
                    "tool": tool_name,
                    "result": "",
                    "error": f"tool descriptor '{descriptor_name}' is missing or invalid",
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
        return _observation_candidate(self.worker_id, contract, obs)


def _observation_candidate(
    worker_id: str, contract: Contract, observation: str
) -> Candidate:
    if len(contract.outputs) != 1:
        return Candidate(
            worker_id=worker_id,
            raw_output="tool method contract must declare exactly one observation output",
        )
    outputs = {contract.outputs[0]: observation}
    return Candidate(
        worker_id=worker_id,
        raw_output=json.dumps(
            {"type": "exec", "outputs": outputs},
            sort_keys=True,
            ensure_ascii=False,
        ),
        parsed_action={"type": "exec", "outputs": outputs},
    )
