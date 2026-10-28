"""Bulk reassign stays at the CLI and signed assignment-owner boundary."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_reassign_batch_uses_existing_signed_owner_and_exact_predecessor_map():
    path = ROOT / "src/aigineering/cli/task_assignment.py"
    source = path.read_text(encoding="utf-8")
    batch = ast.unparse(_function(path, "_task_reassign_batch"))
    target_owner = ast.unparse(_function(path, "_collect_reassign_targets"))
    command = ast.unparse(_function(path, "task_reassign"))
    key = ast.unparse(_function(path, "_reassign_target_key"))

    assert "_task_reassign_batch" in command
    assert "len(targets) >= 2" in command
    assert "_missing_reassign_parameter" in command
    assert "--previous" in source
    assert "--previous-for" in source
    assert "exact_target_mapping" in batch
    assert "_collect_reassign_targets" in batch
    assert "validate_task_assignment" in target_owner
    assert "publish_task_assignment" in target_owner
    assert "collect_task_batch" in target_owner
    assert "require_assigned=True" in target_owner
    assert "requested_previous_assignment_id" in target_owner
    assert "previous_assignment_id" in key
    assert "canonical_json" in key
    assert "CandidatePublisher" not in source
    assert "publish_effect" not in source
    assert "SQLiteStore" not in source


def test_reassignment_batch_does_not_change_the_shared_collector_contract():
    path = ROOT / "src/aigineering/cli/_task_batch.py"
    collector = _function(path, "collect_task_batch")
    signature = ast.unparse(collector.args)
    body = ast.unparse(collector)
    assert "expected_rejection_types" in signature
    assert "key_for_target" in signature
    assert "RedisSyncPending" in body
    assert "_not_attempted_tail" in body
