"""Compile feedback has one pure format owner and retains recovery ownership."""

import ast
from pathlib import Path


def test_compile_diagnostics_are_pure_and_shared_by_writer_and_proof_reader():
    root = Path("src/aigineering")
    owner = (root / "planning_diagnostics.py").read_text()
    imports = {
        node.module
        for node in ast.walk(ast.parse(owner))
        if isinstance(node, ast.ImportFrom)
    }
    assert imports <= {"__future__", "collections.abc"}
    assert "store" not in {
        node.id for node in ast.walk(ast.parse(owner)) if isinstance(node, ast.Name)
    }
    for name in ("runtime.py", "task_diagnostics.py"):
        tree = ast.parse((root / name).read_text())
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "aigineering.planning_diagnostics"
            and "valid_compile_diagnostics" in {alias.name for alias in node.names}
            for node in ast.walk(tree)
        )
    # Kernel admission and generic recovery do not interpret diagnostic fields.
    assert all(
        "compile_diagnostics" not in path.read_text()
        for path in (root / "core").glob("*.py")
    )
    assert "compile_diagnostics" not in (root / "plugins/recovery.py").read_text()
