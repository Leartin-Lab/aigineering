"""Task context inspection remains a read-only application boundary."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def test_task_context_owner_has_no_cli_or_mutation_dependencies():
    tree = _tree("src/aigineering/task_context.py")
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    )
    assert not any(name.startswith("aigineering.cli") for name in imports)
    assert not any(
        name.startswith(
            ("aigineering.core.candidate_publisher", "aigineering.task_publication")
        )
        for name in imports
    )
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls.intersection(
        {
            "add_asset",
            "add_contract",
            "append_runtime_record",
            "commit_ingress_batch",
            "commit_candidate",
            "new_entry",
            "claim_contract",
            "commit_worker_invocation_failure",
            "publish_task_contract",
        }
    )
    assert "aigineering.core.disclosure.compute_disclosure" in imports


def test_runtime_compatibility_helper_delegates_to_shared_owner():
    tree = _tree("src/aigineering/runtime.py")
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_method_context_assets_for"
    ]
    assert len(matches) == 1
    function = matches[0]
    assert len(function.body) == 1
    statement = function.body[0]
    assert isinstance(statement, ast.Return)
    assert isinstance(statement.value, ast.Call)
    assert isinstance(statement.value.func, ast.Name)
    assert statement.value.func.id == "method_context_assets_for"


def test_task_context_is_registered_on_existing_task_group():
    tree = _tree("src/aigineering/cli/task.py")
    registrations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_task_context_command"
    ]
    assert len(registrations) == 1
