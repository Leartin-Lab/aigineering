"""Tool method handler — extracts tool lifecycle out of Engine (v0.3.5)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.agent.tool_worker import ToolWorker
from aigineering.core.methods import method_payload, system_asset
from aigineering.core.provenance import sign_asset

if TYPE_CHECKING:
    from aigineering.core.engine import Engine
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
        engine: Engine,
        contract: Contract,
        action_type: str,
        candidate: Candidate,
    ) -> bool:
        from aigineering.core.engine import _parse_method_action

        action = _parse_method_action(candidate)
        if action is None:
            return False
        engine._schedule_method_contract(contract, action, candidate)
        return True

    def handle_completion(
        self,
        engine: Engine,
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

        call_asset_name = f"_tool_call_{contract.id}"
        existing = engine._store.get_assets_by_name(call_asset_name)
        if existing:
            return True

        tool_name = payload.get("payload", {}).get("name") if isinstance(payload.get("payload"), dict) else None
        args = payload.get("payload", {}).get("args", {}) if isinstance(payload.get("payload"), dict) else {}

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
        call_asset = system_asset(
            name=f"_tool_call_{contract.id}",
            content=call_content,
            created_by=contract.id,
            promptable=False,
        )

        ok = False
        result = ""
        error = ""
        if not isinstance(tool_name, str) or not tool_name:
            error = "tool action missing string payload.name"
        elif tool_name not in contract.tool_scope:
            error = f"tool '{tool_name}' is not in contract.tool_scope"
        elif engine._tools is None:
            error = "no ToolRegistry configured"
        else:
            worker = ToolWorker(engine._tools)
            candidate = worker.invoke(
                tool_name,
                args if isinstance(args, dict) else {},
                contract.id,
            )
            obs = json.loads(candidate.raw_output)
            ok = obs.get("ok", False)
            result = obs.get("result", "")
            error = obs.get("error", "")

        obs_name = contract.outputs[0] if contract.outputs else f"_tool_obs_{contract.id}"
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
        obs_asset = system_asset(
            name=obs_name,
            content=obs_content,
            created_by=contract.id,
            source_uri=f"tool://{tool_name}" if isinstance(tool_name, str) else "",
        )

        engine._store.add_asset(sign_asset(call_asset))
        engine._store.add_asset(sign_asset(obs_asset))
        engine._add_trace(
            contract.id,
            "tool_executed",
            accepted_fragments=[call_asset.id, obs_asset.id],
            accepted_asset_names=[call_asset.name, obs_asset.name],
            authority_result="accepted" if ok else "rejected",
            relation_type="tool",
            relation_target=tool_name if isinstance(tool_name, str) else None,
            budget_remaining=engine._resolve_budget(contract),
        )
        return True
