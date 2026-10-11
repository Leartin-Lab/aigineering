"""Pool, target, and Fleet share one read-only invocation-failure owner."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _calls(node):
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def test_run_and_fleet_reuse_shared_exact_claim_diagnostics_owner():
    owner_path = ROOT / "src/aigineering/run_failure_diagnostics.py"
    owner = ast.parse(owner_path.read_text())
    owner_imports = {
        alias.name
        for node in ast.walk(owner)
        if isinstance(node, ast.ImportFrom)
        and node.module == "aigineering.task_diagnostics"
        for alias in node.names
    }
    assert owner_imports == {"task_invocation_failure_binding"}
    assert {
        node.name
        for node in ast.walk(owner)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } == {"invocation_failure_detail", "format_worker_failure_detail"}
    assert not _calls(owner) & {
        "commit_local_effect",
        "commit_local_effects",
        "submit_worker_proposal",
        "sync_runtime",
    }

    fleet = ast.parse((ROOT / "src/aigineering/local_fleet.py").read_text())
    fleet_imports = {
        alias.name
        for node in ast.walk(fleet)
        if isinstance(node, ast.ImportFrom)
        and node.module == "aigineering.run_failure_diagnostics"
        for alias in node.names
    }
    assert fleet_imports == {
        "format_worker_failure_detail",
        "invocation_failure_detail",
    }
    assert "task_invocation_failure_binding" not in {
        node.func.id
        for node in ast.walk(fleet)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    run = ast.parse((ROOT / "src/aigineering/cli/run.py").read_text())
    run_imports = {
        alias.name
        for node in ast.walk(run)
        if isinstance(node, ast.ImportFrom)
        and node.module == "aigineering.run_failure_diagnostics"
        for alias in node.names
    }
    assert run_imports == {
        "format_worker_failure_detail",
        "invocation_failure_detail",
    }
    for function_name in ("_run_single_pool_cycle", "_run_target_pool"):
        function = next(
            node
            for node in run.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        assert "invocation_failure_detail" in _calls(function)
        assert "execute_captured_package" in _calls(function)
