"""Keep signed task publication owned by the application service."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tree(relative_path: str) -> ast.Module:
    path = ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_task_publication_service_owns_contract_build_and_signed_effect_path():
    service = _tree("src/aigineering/task_publication.py")
    imports = _imports(service)
    assert not any(
        name == "aigineering.cli" or name.startswith("aigineering.cli.")
        for name in imports
    )
    assert "aigineering.core.sqlite_store" not in imports
    assert "aigineering.local_publication" in imports

    source = (ROOT / "src/aigineering/task_publication.py").read_text(encoding="utf-8")
    for direct_write in (
        ".add_contract(",
        ".append_runtime_record(",
        ".commit_ingress_batch(",
    ):
        assert direct_write not in source
    assert "def prepare_task_contract(" in source
    assert "def publish_task_contract(" in source


def test_task_contract_and_retry_commands_delegate_publication_to_service():
    call_sites = (
        ("src/aigineering/cli/task.py", "task_create"),
        ("src/aigineering/cli/contract.py", "contract_add"),
    )
    for path, function_name in call_sites:
        tree = _tree(path)
        function = _function(tree, function_name)
        called = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "publish_task_contract" in called

    tree = _tree("src/aigineering/cli/retry.py")
    retry_call = _function(tree, "retry")
    called = {
        node.func.id
        for node in ast.walk(retry_call)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "retry_local_task_result" in called

    publication = _tree("src/aigineering/task_publication.py")
    retry = _function(publication, "retry_local_task")
    assert "_retry_local_task_operation" in {
        node.func.id
        for node in ast.walk(retry)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    result = _function(publication, "retry_local_task_result")
    assert "_retry_local_task_operation" in {
        node.func.id
        for node in ast.walk(result)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for path, function_name in call_sites[:2]:
        tree = _tree(path)
        function = _function(tree, function_name)
        called = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "prepare_task_contract" in called
