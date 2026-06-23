"""Tool method handler — extracts tool lifecycle out of Engine (v0.3.5)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.agent.mcp_executor import MCPExecutor
from aigineering.agent.tool_executor import ToolExecutor
from aigineering.core.capability_descriptors import verify_descriptor
from aigineering.core.methods import method_payload
from aigineering.protocol.actions import parse_method_action

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime
    from aigineering.protocol.types import Asset, Candidate, Contract


class ToolMethodHandler:
    """Handler for ``tool`` method actions.

    ``handle_method`` schedules the tool sub-contract using the engine's
    built-in scheduler.  ``handle_completion`` executes the tool (creates
    ``_tool_call_*`` and ``_tool_obs_*`` assets, adds trace events).
    """

    def can_handle(self, action_type: str) -> bool:
        return action_type == "tool"

    def handle_method(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        action_type: str,
        candidate: Candidate,
    ) -> bool:
        action = parse_method_action(candidate)
        if action is None:
            return False
        runtime.schedule_method(contract, action, candidate)
        return True

    def handle_completion(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool:
        """Execute the tool for a tool method contract.

        Called both from :meth:`Engine._run_system_method` (with empty
        *method_assets*) to execute the tool, and from
        :meth:`Engine._resume_parent_from_method` (with actual assets)
        where it returns ``True`` to prevent fallback to plan expansion.
        Idempotent: skips execution when ``_tool_call_*`` already exists.
        """
        payload = method_payload(contract)
        if payload.get("method") != "tool":
            return False

        tool_name = (
            payload.get("payload", {}).get("name")
            if isinstance(payload.get("payload"), dict)
            else None
        )
        args = (
            payload.get("payload", {}).get("args", {})
            if isinstance(payload.get("payload"), dict)
            else {}
        )
        is_mcp_tool = isinstance(tool_name, str) and tool_name.startswith("mcp:")
        call_asset_name = (
            f"_mcp_call_{contract.id}" if is_mcp_tool else f"_tool_call_{contract.id}"
        )
        existing = runtime.get_assets_by_name(call_asset_name)
        if existing:
            return True

        call_content = json.dumps(
            {
                "tool": tool_name,
                "args": args,
                "contract_id": contract.id,
                "parent_contract_id": contract.parent_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

        ok = False
        result = ""
        error = ""
        if not isinstance(tool_name, str) or not tool_name:
            error = "tool action missing string payload.name"
        elif tool_name not in contract.tool_scope:
            error = f"tool '{tool_name}' is not in contract.tool_scope"
        elif tool_name.startswith("mcp:"):
            # ── MCP tool routing ───────────────────────────────────
            mcp_full = tool_name[4:]  # strip "mcp:"
            server_name = mcp_full.split(".", 1)[0] if "." in mcp_full else mcp_full
            mcp_descriptor_name = f"_mcp_{server_name}"
            descriptors = runtime.get_assets_by_name(mcp_descriptor_name)
            if not descriptors:
                error = (
                    f"MCP descriptor '{mcp_descriptor_name}' is missing "
                    "(G10 trust gate)"
                )
            elif not verify_descriptor(descriptors[0], kind="mcp"):
                error = (
                    f"MCP descriptor '{mcp_descriptor_name}' failed "
                    "verification (G10 trust gate)"
                )
            else:
                mcp_servers = runtime.get_mcp_servers()
                worker = MCPExecutor(mcp_servers)
                candidate = worker.invoke(
                    mcp_full,
                    args if isinstance(args, dict) else {},
                    contract.id,
                )
                obs = json.loads(candidate.raw_output)
                ok = obs.get("ok", False)
                result = obs.get("result", "")
                error = obs.get("error", "")
        elif runtime.get_tool_registry() is None:
            error = "no ToolRegistry configured"
        else:
            # ── Regular tool execution ─────────────────────────────
            tool_descriptor_name = f"_tool_capability_{tool_name}"
            descriptors = runtime.get_assets_by_name(tool_descriptor_name)
            if not descriptors:
                error = f"tool '{tool_name}' descriptor is missing (G10 trust gate)"
            elif not verify_descriptor(descriptors[0], kind="tool"):
                error = (
                    f"tool '{tool_name}' descriptor failed verification "
                    "(G10 trust gate)"
                )
            else:
                worker = ToolExecutor(runtime.get_tool_registry())
                candidate = worker.invoke(
                    tool_name,
                    args if isinstance(args, dict) else {},
                    contract.id,
                )
                obs = json.loads(candidate.raw_output)
                ok = obs.get("ok", False)
                result = obs.get("result", "")
                error = obs.get("error", "")

        obs_name = (
            contract.outputs[0] if contract.outputs else f"_tool_obs_{contract.id}"
        )
        obs_content = json.dumps(
            {
                "ok": ok,
                "tool": tool_name,
                "result": result,
                "error": error,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

        # Mint the call system asset (promptable=False — internal artifact)
        call_asset = runtime.mint_system_asset(
            call_asset_name,
            call_content,
            contract.id,
            promptable=False,
        )

        # Mint the observation system asset
        obs_asset = runtime.mint_system_asset(
            obs_name,
            obs_content,
            contract.id,
            source_uri=f"tool://{tool_name}" if isinstance(tool_name, str) else "",
        )

        runtime.append_trace(
            contract.id,
            "tool_executed",
            accepted_fragments=[call_asset.id, obs_asset.id],
            accepted_asset_names=[call_asset.name, obs_asset.name],
            authority_result="accepted" if ok else "rejected",
            relation_type="tool",
            relation_target=tool_name if isinstance(tool_name, str) else None,
            budget_remaining=runtime.resolve_budget(contract.id),
        )
        return True
