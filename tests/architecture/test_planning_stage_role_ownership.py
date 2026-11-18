"""Planning-stage routing has one semantic owner and preserves old proofs."""

from __future__ import annotations

import inspect

from aigineering.agent import prompt, worker
from aigineering.plugins import delegation, planning_roles, recovery, staged_planning


def test_prompt_and_worker_use_the_shared_active_stage_owner():
    assert "active_planning_stage" in inspect.getsource(prompt)
    assert "active_planning_stage" in inspect.getsource(worker)
    assert "plugin:plan.compile" not in inspect.getsource(worker)
    assert "plugin:replan.compile" not in inspect.getsource(worker)


def test_new_method_children_and_nested_stages_drop_inherited_stage_controls():
    assert "without_planning_stage_labels" in inspect.getsource(delegation)
    assert "without_planning_stage_labels" in inspect.getsource(staged_planning)
    assert "PLANNING_STAGE_LABELS" in inspect.getsource(planning_roles)


def test_recovery_uses_actual_publications_without_legacy_contract_makers():
    source = inspect.getsource(recovery)
    assert "_claimed_method_contract" not in source
    assert "_legacy_parallel_tool_contracts" not in source
    assert "_canonical_method_source" not in source
    assert "_capture_recovery_history(" in source
    assert "source_binding" in source
