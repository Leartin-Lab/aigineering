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
