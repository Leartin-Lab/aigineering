"""Architecture gates for stateless task-completion projection."""

from __future__ import annotations

import ast
from pathlib import Path

from conftest import candidate_runtime

from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.core.trace_manager import TraceManager
from aigineering.plugins.completion_projection import TaskCompletionContext
from aigineering.protocol.types import Contract


def test_production_runtime_does_not_import_legacy_lifecycle_owners():
    source = Path("src/aigineering/runtime.py").read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "ContinuationManager" not in imports
    assert "MethodRuntime" not in imports


def test_task_completion_projector_has_no_waiting_or_resume_state():
    source = Path("src/aigineering/plugins/completion_projection.py").read_text()
    forbidden = ("_suspended", "_method_scheduled", "resume_parent_from_method")
    assert all(name not in source for name in forbidden)


def test_completion_context_rebuilds_remaining_allowance_from_facts():
    store = MemoryStore()
    trace = MemoryTraceStore()
    runtime = candidate_runtime(store, trace)
    parent = runtime.accept_contract(
        Contract(id="", name="parent", outputs=("result",), budget=5)
    )
    runtime.accept_contract(
        Contract(
            id="",
            parent_id=parent.id,
            name="child",
            outputs=("child_result",),
            budget=2,
        )
    )

    first = TaskCompletionContext(store, TraceManager(trace), None)
    rebuilt = TaskCompletionContext(store, TraceManager(trace), None)

    assert first.resolve_budget(parent.id) == 3
    assert rebuilt.resolve_budget(parent.id) == 3


def test_budget_manager_is_not_a_production_runtime_owner():
    assert not Path("src/aigineering/core/budget_manager.py").exists()
    for path in Path("src/aigineering").rglob("*.py"):
        assert "BudgetManager" not in path.read_text(), path


def test_legacy_empty_input_construction_is_history_recognition_only():
    root = Path("src/aigineering")
    callers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "continuation_contract"
                and any(key.arg == "legacy_empty_inputs" for key in node.keywords)
            ):
                callers.add(path.relative_to(root).as_posix())
    assert callers == {"plugins/continuation_history.py"}
    history = ast.parse((root / "plugins/continuation_history.py").read_text())
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"publish", "add_contract", "append_runtime_record"}
        for node in ast.walk(history)
    )


def test_recovery_source_proof_stays_read_only_and_publishes_through_runtime():
    runtime_tree = ast.parse(Path("src/aigineering/runtime.py").read_text())
    runtime_function = next(
        node
        for node in runtime_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_schedule_rejected_recovery"
    )
    calls = [node for node in ast.walk(runtime_function) if isinstance(node, ast.Call)]
    called_names = {node.func.id for node in calls if isinstance(node.func, ast.Name)}
    assert "task_recovery_source_binding" in called_names
    assert "task_recovery_ancestor_source_bindings" in called_names
    schedule_call = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "schedule_projection_recovery"
    )
    assert schedule_call.args and isinstance(schedule_call.args[0], ast.Name)
    assert schedule_call.args[0].id == "runtime"
    assert not any(
        isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {
            "publish",
            "publish_task_effects",
            "append_runtime_record",
            "commit_candidate",
        }
        for node in calls
    )

    query_tree = ast.parse(Path("src/aigineering/task_view_query.py").read_text())
    query_context = next(
        node
        for node in query_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TaskViewQueryContext"
    )
    require_match = next(
        node
        for node in query_context.body
        if isinstance(node, ast.FunctionDef) and node.name == "require_match"
    )
    comparisons = [
        node for node in ast.walk(require_match) if isinstance(node, ast.Compare)
    ]
    assert any(
        isinstance(node.left, ast.Name)
        and node.left.id == "store"
        and any(
            isinstance(comparator, ast.Attribute) and comparator.attr == "source_store"
            for comparator in node.comparators
        )
        for node in comparisons
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_runtime_revision"
        for node in ast.walk(require_match)
    )

    recovery_tree = ast.parse(Path("src/aigineering/plugins/recovery.py").read_text())
    recovery_calls = [
        node for node in ast.walk(recovery_tree) if isinstance(node, ast.Call)
    ]
    assert any(
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "publish_task_effects"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "runtime"
        for node in recovery_calls
    )
