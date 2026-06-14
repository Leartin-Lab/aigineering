"""Plan method handler — extracts plan logic out of Engine (v0.3.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.disclosure import compute_disclosure
from aigineering.core.methods import contracts_from_plan_asset, method_payload
from aigineering.protocol.actions import parse_method_action

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime
    from aigineering.protocol.types import Asset, Candidate, Contract


class PlanMethodHandler:
    """Handler for ``plan`` method actions.

    ``handle_method`` schedules the plan sub-contract using the engine's
    built-in scheduler (so the existing tooling / method-contract machinery
    is reused).  ``handle_completion`` performs plan-result expansion
    (child-contract creation with containment checks).
    """

    def can_handle(self, action_type: str) -> bool:
        return action_type == "plan"

    def handle_method(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        action_type: str,
        candidate: Candidate,
    ) -> bool:
        # Re-parse the full WorkerAction from the candidate so we can use
        # the standard schedule_method path.
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
        """Expand plan results into non-system child contracts.

        Returns True when at least one ``_plan_result_*`` asset was found
        and expansion was attempted (even if all children were rejected by
        containment checks).
        """
        if method_payload(contract).get("method") != "plan":
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
                    relation_type="plan",
                    relation_target="parent_not_found",
                    rejected_fragments=[
                        "[rejected] parent_not_found: "
                        f"parent contract {parent_id} not in store — "
                        "plan expansion abort (fail-closed)"
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
        for asset in method_assets:
            if not asset.name.startswith("_plan_result_"):
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
                    relation_type="plan",
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

        if created and parent_id is not None:
            runtime.append_trace(
                parent_id,
                "contracts_expanded",
                relation_type="plan",
                relation_target=",".join(created),
                budget_remaining=runtime.resolve_budget(parent_id),
            )

        return expanded
