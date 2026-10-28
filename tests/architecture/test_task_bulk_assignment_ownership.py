"""Bulk initial assignment stays at the CLI/app boundary."""

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


def test_assignment_batch_uses_signed_application_owner_and_narrow_errors():
    cli_path = ROOT / "src/aigineering/cli/task_assignment.py"
    cli = cli_path.read_text(encoding="utf-8")
    task_assign = ast.unparse(_function(cli_path, "task_assign"))
    batch = ast.unparse(_function(cli_path, "_task_assign_batch"))
    assert "_task_assign_batch" in task_assign
    assert "targets[0]" in task_assign
    assert "publish_task_assignment" in batch
    assert "collect_task_batch" in batch
    assert "previous_assignment_id=''" in batch
    assert "require_unassigned=True" in batch
    assert "expected_rejection_types" in batch
    assert "AssignmentOperationError" in batch
    assert "CandidatePublisher" not in cli
    assert "publish_effect" not in cli


def test_assignment_operation_error_preserves_valueerror_compatibility():
    app_path = ROOT / "src/aigineering/task_assignment.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    definition = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AssignmentOperationError"
    )
    assert any(
        isinstance(base, ast.Name) and base.id == "ValueError"
        for base in definition.bases
    )


def test_collector_has_compatibility_default_and_safe_batch_key_switch():
    path = ROOT / "src/aigineering/cli/_task_batch.py"
    source = path.read_text(encoding="utf-8")
    collector = _function(path, "collect_task_batch")
    signature = ast.unparse(collector.args)
    assert "expected_rejection_types" in signature
    assert "ValueError" in signature
    assert "passthrough_exceptions" in signature
    emitter = ast.unparse(_function(path, "emit_batch"))
    assert "safe_human_base" in emitter
    assert "fullmatch" in source
