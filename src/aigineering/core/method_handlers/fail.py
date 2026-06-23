"""Fail method handler — restores /fail as a first-class method action (v0.5.0)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.core.methods import method_payload
from aigineering.protocol.actions import parse_method_action

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime
    from aigineering.protocol.types import Asset, Candidate, Contract


class FailMethodHandler:
    """Handler for ``fail`` method actions.

    ``handle_method`` schedules the fail sub-contract using the engine's
    built-in scheduler.  ``handle_completion`` processes failure result
    assets into observable ``_fail_report_*`` system assets.
    """

    def can_handle(self, action_type: str) -> bool:
        return action_type == "fail"

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
        """Process failure result assets into ``_fail_report_*`` system assets.

        Called both from the engine's system-method path (with empty
        *method_assets*) and from parent-resume (with actual assets).
        Idempotent: skips when ``_fail_report_{contract.id}`` already exists.
        """
        payload = method_payload(contract)
        if payload.get("method") != "fail":
            return False

        report_name = f"_fail_report_{contract.id}"
        existing = runtime.get_assets_by_name(report_name)
        if existing:
            return True

        # Collect failure result content from method_assets (parent-resume path)
        for asset in method_assets:
            if asset.name.startswith("_fail_result_"):
                runtime.mint_system_asset(
                    name=report_name,
                    content=asset.content,
                    created_by=contract.id,
                    promptable=True,
                )

                parent_id = contract.parent_id
                if parent_id is not None:
                    runtime.append_trace(
                        parent_id,
                        "fail_reported",
                        relation_type="fail",
                        relation_target=contract.id,
                        authority_result="accepted",
                        budget_remaining=runtime.resolve_budget(parent_id),
                    )
                return True

        # system-method path: generate a default fail report from payload
        fail_payload = payload.get("payload", {})
        if not isinstance(fail_payload, dict):
            fail_payload = {}
        reason = fail_payload.get("reason", "unspecified failure")
        detail = fail_payload.get("detail", "")
        parent_name = fail_payload.get("parent_contract_name", "unknown")

        report = runtime.mint_system_asset(
            name=report_name,
            content=json.dumps(
                {
                    "method": "fail",
                    "contract_id": contract.id,
                    "parent_contract_id": contract.parent_id,
                    "parent_contract_name": parent_name,
                    "reason": reason,
                    "detail": detail,
                },
                sort_keys=True,
                ensure_ascii=False,
            ),
            created_by=contract.id,
            promptable=True,
        )

        parent_id = contract.parent_id
        if parent_id is not None:
            runtime.append_trace(
                parent_id,
                "fail_reported",
                relation_type="fail",
                relation_target=contract.id,
                accepted_fragments=[report.id],
                accepted_asset_names=[report.name],
                authority_result="accepted",
                budget_remaining=runtime.resolve_budget(parent_id),
            )

        return True
