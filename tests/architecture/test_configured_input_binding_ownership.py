from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_configured_run_uses_shared_input_binding_owner_for_new_roots_only():
    source = ROOT / "src/aigineering/configured_run.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
    assert "aigineering.core.input_bindings" in imports

    publish_root = _function(tree, "_publish_root")
    called = {
        node.func.id
        for node in ast.walk(publish_root)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_validate_prepared_input_assets" in called
    assert "_published_input_asset_bindings" in called
    assert "input_asset_bindings" in called

    run_configured = _function(tree, "run_configured")
    resume_branch = next(
        node
        for node in ast.walk(run_configured)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Attribute)
        and node.test.left.attr == "resume_task_id"
    )
    resume_calls = {
        node.func.id
        for statement in resume_branch.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    new_run_calls = {
        node.func.id
        for statement in resume_branch.orelse
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_publish_root" not in resume_calls
    assert "_publish_root" in new_run_calls
