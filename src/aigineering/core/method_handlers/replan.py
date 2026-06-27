"""Replan method handler — extracts replan logic out of Engine (v0.4.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.disclosure import compute_disclosure
from aigineering.core.method_handlers.recovery import (
    has_recoverable_method_result_rejection,
    schedule_method_result_recovery,
)
from aigineering.core.methods import contracts_from_plan_asset, method_payload
from aigineering.protocol.actions import parse_method_action

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime
    from aigineering.protocol.types import Asset, Candidate, Contract


class ReplanMethodHandler:
    """Handler for ``replan`` method actions.

    ``handle_method`` schedules the replan sub-contract using the runtime's
    built-in scheduler (so the existing tooling / method-contract machinery
    is reused).  ``handle_completion`` performs replan-result expansion
    (revised child-contract creation with containment checks), analogous
    to :class:`PlanMethodHandler` but operating on ``_replan_result_*`` assets.
    """

    def can_handle(self, action_type: str) -> bool:
        return action_type == "replan"

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
        """Expand replan results into revised non-system child contracts.

        Returns True when at least one ``_replan_result_*`` asset was found
        and expansion was attempted (even if all children were rejected by
        containment checks).
        """
        if method_payload(contract).get("method") != "replan":
            return False

        parent_id = contract.parent_id

        # Fail-closed: if parent_id is set but parent is not in store,
        # do NOT expand at all.
        parent_contract: Contract | None = None
        if parent_id is not None:
            parent_contract = runtime.get_contract(parent_id)
            if parent_contract is None:
                runtime.append_trace(
                    parent_id,
                    "containment_rejected",
                    relation_type="replan",
                    relation_target="parent_not_found",
                    rejected_fragments=[
                        "[rejected] parent_not_found: "
                        f"parent contract {parent_id} not in store — "
                        "replan expansion abort (fail-closed)"
                    ],
                    authority_result="rejected",
                    budget_remaining=0,
                )
                return True  # handled (fail-closed), prevent fallback duplication

        # Compute parent's disclosure scope for input/activation containment.
        allowed_input_names: set[str] | None = None
        parent_budget_remaining: int | None = None
        if parent_contract is not None:
            scope = compute_disclosure(parent_contract, runtime.store)
            allowed_input_names = {a.name for a in scope}
            parent_budget_remaining = runtime.resolve_budget(parent_contract.id)

        expanded = False
        created: list[str] = []
        recovery_scheduled = False
        for asset in method_assets:
            if not asset.name.startswith("_replan_result_"):
                continue
            expanded = True
            children, rejections = contracts_from_plan_asset(
                asset,
                parent_id,
                parent_contract=parent_contract,
                allowed_input_names=allowed_input_names,
                parent_budget_remaining=parent_budget_remaining,
            )
            for child in children:
                if runtime.get_contract(child.id) is None:
                    runtime.add_contract(child)
                    created.append(child.id)
            for entry in rejections:
                runtime.append_trace(
                    parent_id,
                    "containment_rejected",
                    relation_type="replan",
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
            if (
                parent_id is not None
                and not children
                and has_recoverable_method_result_rejection(rejections)
            ):
                recovery = schedule_method_result_recovery(
                    runtime,
                    method_type="replan",
                    parent_id=parent_id,
                    failed_contract=contract,
                    result_asset=asset,
                    rejections=rejections,
                )
                recovery_scheduled = recovery_scheduled or recovery is not None

        if created and parent_id is not None:
            runtime.append_trace(
                parent_id,
                "contracts_expanded",
                relation_type="replan",
                relation_target=",".join(created),
                budget_remaining=runtime.resolve_budget(parent_id),
            )

        return expanded or recovery_scheduled
