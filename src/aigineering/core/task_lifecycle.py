"""Shared pure task-lifecycle relationship predicates."""

from __future__ import annotations

from collections.abc import Mapping, Set
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aigineering.protocol.types import Contract


def serves_pending_independent_acceptance(
    child: Contract,
    completed_parent: Contract,
    contracts_by_id: Mapping[str, Contract],
    terminal_contract_ids: Set[str],
) -> bool:
    """Whether a live verifier child still serves a live independent ancestor.

    Keep this predicate shared by normal completion and handoff traversal so
    the exceptional verifier branch has one semantic owner.
    """
    ancestor_id = completed_parent.parent_id
    visited: set[str] = set()
    while ancestor_id and ancestor_id not in visited:
        visited.add(ancestor_id)
        ancestor = contracts_by_id.get(ancestor_id)
        if ancestor is None:
            return False
        policy = ancestor.acceptance_policy
        verifier_capabilities = (
            set(policy.get("verifier_capabilities", ()))
            if policy is not None and policy.get("mode") == "independent"
            else set()
        )
        if (
            ancestor.id not in terminal_contract_ids
            and verifier_capabilities
            and verifier_capabilities.issubset(child.worker_capabilities)
            and set(ancestor.outputs).issubset(child.inputs)
        ):
            return True
        ancestor_id = ancestor.parent_id
    return False
