"""Keep bounded local execution in the shared application cycle owner."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls(node: ast.AST) -> set[str]:
    return {
        item.func.id
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    }


def test_three_once_entrypoints_delegate_without_owning_execution_loops():
    configured = _functions(ROOT / "src/aigineering/configured_run.py")
    run_cli = _functions(ROOT / "src/aigineering/cli/run.py")
    fleet_cli = _functions(ROOT / "src/aigineering/cli/fleet.py")

    assert "run_local_cycle" in _calls(configured["run_configured"])
    assert "run_local_fleet" in _calls(configured["run_configured"])
    assert "run_local_cycle" in _calls(run_cli["_run_task_pool"])
    assert "_run_single_pool_cycle" not in run_cli
    assert "run_configured" in _calls(fleet_cli["fleet_run"])
    assert "claim_next_application_package" not in _calls(run_cli["_run_task_pool"])
    assert "execute_captured_package" not in _calls(run_cli["_run_task_pool"])
    assert "execute_captured_package" not in _calls(fleet_cli["fleet_run"])
