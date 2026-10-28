from __future__ import annotations

from aigineering.core.claim_assignments import AssignmentAdmission
from aigineering.core.effect_projection import EffectHandler
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
from aigineering.plugins.handoff import HANDOFF_EFFECTS
from aigineering.plugins.task_assignment import TASK_ASSIGNMENT_EFFECTS
from aigineering.plugins.task_dependencies import TASK_DEPENDENCY_EFFECTS
from aigineering.plugins.task_review import TASK_REVIEW_EFFECTS
from aigineering.plugins.task_revision import TASK_REVISION_EFFECTS
from aigineering.plugins.worker_profile_promotion import (
    WORKER_PROFILE_PROMOTION_EFFECTS,
)


def test_in_tree_effect_registries_declare_complete_direct_fact_sets():
    registries = {
        **TASK_ASSIGNMENT_EFFECTS,
        **HANDOFF_EFFECTS,
        **TASK_REVISION_EFFECTS,
        **TASK_REVIEW_EFFECTS,
        **TASK_DEPENDENCY_EFFECTS,
        **COLLABORATION_EFFECTS,
        **WORKER_PROFILE_PROMOTION_EFFECTS,
    }
    expected = {
        "task.assign": {"claim.assignment.changed"},
        "task.handoff": {
            "lifecycle.terminal",
            "contract.declared",
            "claim.assignment.changed",
            "claim.hold.placed",
            "allowance.returned",
            "task.handed_off",
        },
        "task.revise": {
            "lifecycle.terminal",
            "contract.declared",
            "claim.assignment.changed",
            "task.revised",
        },
        "task.review.request": {"contract.declared", "task.review.requested"},
        "task.review.decide": {
            "asset.attested",
            "output.qualified",
            "task.review.decided",
        },
        "task.dependency.link": {"claim.constraint.placed"},
        "task.dependency.unlink": {"claim.constraint.released"},
        "task.comment": {"task.commented"},
        "task.block": {"claim.hold.placed"},
        "task.unblock": {"claim.hold.released"},
        "task.promote": {"claim.hold.released", "task.promoted"},
        "worker.profile.promote": {"worker.profile.promoted"},
    }
    assert set(registries) == set(expected)
    for effect_type, direct_types in expected.items():
        handler = registries[effect_type]
        assert isinstance(handler, EffectHandler)
        assert handler.direct_output_fact_types == frozenset(direct_types)
        reserved = {"task.promoted"} if effect_type == "task.promote" else set()
        assert handler.reserved_fact_types == frozenset(reserved)


def test_assignment_metadata_remains_separate_from_direct_output_ownership():
    assert TASK_ASSIGNMENT_EFFECTS["task.assign"].emitted_fact_types == frozenset(
        {"claim.assignment.changed"}
    )
    assert (
        TASK_ASSIGNMENT_EFFECTS["task.assign"].assignment_admission
        is AssignmentAdmission.DIRECT
    )
    assert HANDOFF_EFFECTS["task.handoff"].emitted_fact_types == frozenset(
        {"claim.assignment.changed"}
    )
    assert (
        HANDOFF_EFFECTS["task.handoff"].assignment_admission
        is AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
    )
    assert TASK_REVISION_EFFECTS["task.revise"].emitted_fact_types == frozenset(
        {"claim.assignment.changed"}
    )
    assert (
        TASK_REVISION_EFFECTS["task.revise"].assignment_admission
        is AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
    )
    assert (
        WORKER_PROFILE_PROMOTION_EFFECTS["worker.profile.promote"].emitted_fact_types
        == frozenset()
    )
