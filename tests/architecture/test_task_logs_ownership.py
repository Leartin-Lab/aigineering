"""Local Worker logs remain auxiliary observations, not runtime state."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def _imports(tree: ast.Module) -> set[str]:
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names.update(
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    )
    return names


def test_task_log_spool_has_no_fact_or_candidate_writer_dependencies():
    tree = _tree("src/aigineering/task_logs.py")
    imports = _imports(tree)
    assert not any(
        name.startswith(
            (
                "aigineering.core.commitment",
                "aigineering.core.candidate_publisher",
                "aigineering.task_publication",
                "aigineering.protocol.effect_builders",
            )
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
            "append_runtime_record",
            "new_entry",
            "add_asset",
            "add_contract",
            "claim_contract",
            "commit_candidate",
            "publish_task_contract",
        }
    )


def test_task_log_cli_uses_diagnostics_claim_binding_and_read_only_snapshot():
    tree = _tree("src/aigineering/cli/task_logs.py")
    imports = _imports(tree)
    assert "aigineering.task_diagnostics.task_log_binding" in imports
    assert "aigineering.task_logs.read_capture" in imports
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "task_log_binding" in calls
    assert "read_capture" in calls
    assert "SQLiteStore.snapshot_from_path" in {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }


def test_log_command_is_registered_on_existing_task_group():
    tree = _tree("src/aigineering/cli/task.py")
    registrations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_task_log_command"
    ]
    assert len(registrations) == 1
