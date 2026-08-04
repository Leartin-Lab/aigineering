"""Plan and replan completion projection plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.plugins.recovery import (
    has_recoverable_method_result_rejection,
    schedule_method_result_recovery,
)
from aigineering.plugins.task_semantics import method_payload
from aigineering.plugins.planning import (
    PlanningExpansionPlugin,
    is_blocking_plan_rejection,
)
from aigineering.plugins.base import PluginRequest

if TYPE_CHECKING:
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.protocol.types import Asset, Contract


class PlanningCompletionPlugin:
    """Project a completed plan task into contained ordinary tasks."""

    action_type = "plan"
    result_prefix = "_plan_result_"

    def can_handle(self, action_type: str) -> bool:
        return action_type == self.action_type

    def handle_completion(
        self,
        runtime: TaskCompletionContext,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool:
        stage_label = f"plugin:{self.action_type}.compile"
        if (
            method_payload(contract).get("method") != self.action_type
            and stage_label not in contract.labels
        ):
            return False

        parent_id = contract.parent_id
        if parent_id is None:
            return False
        parent_contract = runtime.get_contract(parent_id)
        if parent_contract is None:
            runtime.append_trace(
                parent_id,
                "containment_rejected",
                relation_type=self.action_type,
                relation_target="parent_not_found",
                rejected_fragments=[
                    "[rejected] parent_not_found: "
                    f"parent contract {parent_id} not in store — "
                    f"{self.action_type} expansion abort (fail-closed)"
                ],
                authority_result="rejected",
                budget_remaining=0,
            )
            return True

        scope = runtime.compute_disclosure(parent_contract)
        allowed_input_names = {asset.name for asset in scope}
        parent_budget_remaining = runtime.resolve_budget(parent_contract.id)
        expansion = PlanningExpansionPlugin()

        created: list[str] = []
        recovery_scheduled = False
        for asset in method_assets:
            if not asset.name.startswith(self.result_prefix):
                continue
            proposal = expansion.propose(
                PluginRequest(
                    parent=parent_contract,
                    assets=(asset,),
                    allowed_input_names=frozenset(allowed_input_names),
                    allowance=parent_budget_remaining,
                )
            )
            rejections = [dict(item) for item in proposal.rejections]
            decision = None
            if not runtime.can_publish_candidates(expansion.plugin_id):
                rejections.append(
                    {
                        "child_name": "(candidate)",
                        "field": "publication",
                        "reason": "planning expansion Candidate publisher unavailable",
                        "action": "rejected",
                        "recoverable": True,
                    }
                )
            elif proposal.effects:
                decision = runtime.publish_task_effects(
                    expansion.plugin_id,
                    proposal.effects,
                    idempotency_key=(
                        f"planning:{self.action_type}:{contract.id}:{asset.id}"
                    ),
                    causal_parents=(asset.id,),
                )
            children: list[Contract] = []
            if decision is not None and decision.accepted:
                children = list(decision.contracts)
                created.extend(child.id for child in children)
            elif decision is not None:
                rejection = next(
                    record
                    for record in decision.runtime_records
                    if record.record_type.endswith("rejected")
                )
                rejections.append(
                    {
                        "child_name": "(candidate)",
                        "field": "publication",
                        "reason": str(rejection.payload["reason"]),
                        "action": "rejected",
                        "recoverable": True,
                    }
                )
            for entry in rejections:
                if is_blocking_plan_rejection(entry):
                    entry.setdefault("recoverable", True)
            for entry in rejections:
                runtime.append_trace(
                    parent_id,
                    "containment_rejected",
                    relation_type=self.action_type,
                    relation_target=(
                        f"{entry.get('child_name', '?')}:{entry.get('field', '?')}"
                    ),
                    rejected_fragments=[
                        f"[{entry.get('action', 'rejected')}] "
                        f"{entry.get('field', '?')}: {entry.get('reason', '')}"
                    ],
                    authority_result=entry.get("action", "rejected"),
                    budget_remaining=runtime.resolve_budget(parent_id),
                )
            if parent_id is not None and has_recoverable_method_result_rejection(
                rejections
            ):
                recovery = schedule_method_result_recovery(
                    runtime,
                    method_type=self.action_type,
                    parent_id=parent_id,
                    failed_contract=contract,
                    result_asset=asset,
                    rejections=rejections,
                )
                recovery_scheduled = recovery_scheduled or recovery is not None
                if recovery is None and parent_contract is not None:
                    runtime.fail_contract(
                        parent_contract,
                        reason=(
                            f"{self.action_type} result was rejected and recovery "
                            "could not be published"
                        ),
                        relation_target=contract.id,
                    )
                    return True

        if created and parent_id is not None:
            runtime.append_trace(
                parent_id,
                "contracts_expanded",
                relation_type=self.action_type,
                relation_target=",".join(created),
                budget_remaining=runtime.resolve_budget(parent_id),
            )

        return bool(created) or recovery_scheduled


class ReplanningCompletionPlugin(PlanningCompletionPlugin):
    action_type = "replan"
    result_prefix = "_replan_result_"
