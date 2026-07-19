"""Plan and replan completion projection plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.plugins.recovery import (
    has_recoverable_method_result_rejection,
    schedule_method_result_recovery,
)
from aigineering.plugins.task_semantics import contracts_from_plan_asset, method_payload
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
        if method_payload(contract).get("method") != self.action_type:
            return False

        parent_id = contract.parent_id
        if parent_id is None:
            return False
        parent_contract: Contract | None = None
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

        allowed_input_names: set[str] | None = None
        parent_budget_remaining: int | None = None
        scope = runtime.compute_disclosure(parent_contract)
        allowed_input_names = {asset.name for asset in scope}
        parent_budget_remaining = runtime.resolve_budget(parent_contract.id)

        created: list[str] = []
        recovery_scheduled = False
        for asset in method_assets:
            if not asset.name.startswith(self.result_prefix):
                continue
            decision = None
            published = False
            rejections: list[dict] = []
            if parent_contract is not None:
                plugin_proposal = PlanningExpansionPlugin().propose(
                    PluginRequest(
                        parent=parent_contract,
                        assets=(asset,),
                        allowed_input_names=frozenset(allowed_input_names or ()),
                        allowance=parent_budget_remaining or 0,
                    )
                )
                rejections = [dict(item) for item in plugin_proposal.rejections]
                plugin_id = PlanningExpansionPlugin.plugin_id
                if runtime.can_publish_candidates(plugin_id):
                    published = True
                    if plugin_proposal.effects:
                        decision = runtime.publish_task_effects(
                            plugin_id,
                            plugin_proposal.effects,
                            idempotency_key=(
                                f"planning:{self.action_type}:{contract.id}:{asset.id}"
                            ),
                            causal_parents=(asset.id,),
                        )
            if not published:
                children, rejections = contracts_from_plan_asset(
                    asset,
                    parent_id,
                    parent_contract=parent_contract,
                    allowed_input_names=allowed_input_names,
                    parent_budget_remaining=parent_budget_remaining,
                )
                blocking_rejections = [
                    entry for entry in rejections if is_blocking_plan_rejection(entry)
                ]
                if blocking_rejections:
                    children = []
                for child in children:
                    if runtime.get_contract(child.id) is None:
                        runtime.add_contract(child)
                        created.append(child.id)
            elif decision is None:
                children = []
            elif decision.accepted:
                children = list(decision.contracts)
                created.extend(child.id for child in children)
            else:
                children = []
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
