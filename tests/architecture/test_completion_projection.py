"""Architecture gates for stateless task-completion projection."""

from __future__ import annotations

import ast
from pathlib import Path


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
