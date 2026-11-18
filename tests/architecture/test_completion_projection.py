"""Task completion is derived from ordinary assets and declarations."""

from __future__ import annotations

import ast
from pathlib import Path


def test_completion_controller_and_postcompletion_writers_are_removed():
    root = Path("src/aigineering")
    removed_modules = (
        "plugins/completion.py",
        "plugins/completion_projection.py",
        "plugins/fail_completion.py",
        "plugins/planning_completion.py",
        "plugins/tool_completion.py",
    )
    assert all(not (root / path).exists() for path in removed_modules)
    source = (root / "runtime.py").read_text()
    for symbol in (
        "TaskCompletionContext",
        "TaskCompletionProjector",
        "CompletionRegistry",
        "process_task_completions",
        "_commit_terminal_preserved_outcome",
        "_commit_recovery_outcome",
    ):
        assert symbol not in source


def test_maintenance_is_limited_to_failure_and_rejection_replay():
    tree = ast.parse(Path("src/aigineering/runtime.py").read_text())
    maintenance = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "run_runtime_maintenance_step"
    )
    calls = {
        node.func.id
        for node in ast.walk(maintenance)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls - {"tuple"} == {
        "process_worker_failures",
        "process_rejected_submissions",
        "RuntimeMaintenanceResult",
    }


def test_recovery_publication_is_not_followed_by_runtime_marker_writes():
    tree = ast.parse(Path("src/aigineering/plugins/recovery.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
        assert not any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr in {"append_runtime_record", "append_trace"}
            for call in calls
        ), node.name


def test_budget_is_not_owned_by_a_separate_production_manager():
    root = Path("src/aigineering")
    assert not (root / "core/budget_manager.py").exists()
    assert all("BudgetManager" not in path.read_text() for path in root.rglob("*.py"))
