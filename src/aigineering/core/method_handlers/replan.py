"""Replan method handler — extracts replan logic out of Engine (v0.4.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.disclosure import compute_disclosure
from aigineering.core.methods import contracts_from_plan_asset, method_payload

if TYPE_CHECKING:
    from aigineering.core.engine import Engine
    from aigineering.protocol.types import Asset, Candidate, Contract


class ReplanMethodHandler:
    """Handler for ``replan`` method actions.

    ``handle_method`` schedules the replan sub-contract using the engine's
    built-in scheduler (so the existing tooling / method-contract machinery
    is reused).  ``handle_completion`` performs replan-result expansion
    (revised child-contract creation with containment checks), analogous
    to :class:`PlanMethodHandler` but operating on ``_replan_result_*`` assets.
    """

    def can_handle(self, action_type: str) -> bool:
        return action_type == "replan"

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
            parent_contract = engine._store.get_contract(parent_id)
            if parent_contract is None:
                engine._add_trace(
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
            scope = compute_disclosure(parent_contract, engine._store)
            allowed_input_names = {a.name for a in scope}
            parent_budget_remaining = engine._resolve_budget(parent_contract)

        expanded = False
        created: list[str] = []
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
                if engine._store.get_contract(child.id) is None:
                    engine.add_contract(child)
                    created.append(child.id)
            for entry in rejections:
                engine._add_trace(
                    parent_id,
                    "containment_rejected",
                    relation_type="replan",
                    relation_target=(
                        f"{entry.get('child_name','?')}:{entry.get('field','?')}"
                    ),
                    rejected_fragments=[
                        f"[{entry.get('action','rejected')}] "
                        f"{entry.get('field','?')}: {entry.get('reason','')}"
                    ],
                    authority_result=entry.get("action", "rejected"),
                    budget_remaining=engine._budget.get(parent_id, 0),
                )

        if created and parent_id is not None:
            engine._add_trace(
                parent_id,
                "contracts_expanded",
                relation_type="replan",
                relation_target=",".join(created),
                budget_remaining=engine._budget.get(parent_id, 0),
            )

        return expanded
