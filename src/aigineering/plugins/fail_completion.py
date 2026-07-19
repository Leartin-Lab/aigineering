"""Completion projection for explicit worker failure tasks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.plugins.task_semantics import method_payload, system_asset
from aigineering.protocol.effect_builders import asset_proposal_effect

if TYPE_CHECKING:
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.protocol.types import Asset, Contract


class FailCompletionPlugin:
    """Publish a failure report and close the unfinished parent explicitly."""

    action_type = "fail"
    plugin_id = "fail.report.v1"

    def can_handle(self, action_type: str) -> bool:
        return action_type == self.action_type

    def handle_completion(
        self,
        runtime: TaskCompletionContext,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool:
        payload = method_payload(contract)
        if payload.get("method") != self.action_type:
            return False
        parent = (
            runtime.get_contract(contract.parent_id)
            if contract.parent_id is not None
            else None
        )
        report_name = f"_fail_report_{contract.id}"
        existing = runtime.get_assets_by_name(report_name)
        report = existing[-1] if existing else None
        if report is None:
            content = self._report_content(contract, method_assets, payload)
            proposed = system_asset(
                name=report_name,
                content=content,
                created_by=contract.id,
                promptable=True,
            )
            if runtime.can_publish_candidates(self.plugin_id):
                decision = runtime.publish_task_effects(
                    self.plugin_id,
                    (asset_proposal_effect(proposed),),
                    idempotency_key=f"fail-report:{contract.id}",
                    causal_parents=tuple(asset.id for asset in method_assets),
                )
                if decision is None or not decision.accepted:
                    runtime.record_rejection(
                        contract.id,
                        "failure report Candidate publication was rejected",
                        relation_type="fail",
                        relation_target=report_name,
                        authority_result="rejected",
                    )
                elif decision.assets:
                    report = decision.assets[0]
            else:
                report = runtime.mint_authorized_system_asset(
                    contract,
                    name=report_name,
                    content=content,
                    created_by=contract.id,
                    promptable=True,
                )

        if parent is not None:
            runtime.append_trace(
                parent.id,
                "fail_reported",
                relation_type="fail",
                relation_target=contract.id,
                accepted_fragments=[report.id] if report is not None else [],
                accepted_asset_names=[report.name] if report is not None else [],
                authority_result="accepted" if report is not None else "rejected",
                budget_remaining=runtime.resolve_budget(parent.id),
            )
            runtime.fail_contract(
                parent,
                reason="worker published an explicit fail task",
                relation_target=contract.id,
            )
        return True

    @staticmethod
    def _report_content(contract, method_assets, payload) -> str:
        result = next(
            (
                asset.content
                for asset in method_assets
                if asset.name in contract.outputs
            ),
            None,
        )
        if result is not None:
            return result
        fail_payload = payload.get("payload", {})
        if not isinstance(fail_payload, dict):
            fail_payload = {}
        return json.dumps(
            {
                "method": "fail",
                "contract_id": contract.id,
                "parent_contract_id": contract.parent_id,
                "parent_contract_name": fail_payload.get(
                    "parent_contract_name", "unknown"
                ),
                "reason": fail_payload.get("reason", "unspecified failure"),
                "detail": fail_payload.get("detail", ""),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
