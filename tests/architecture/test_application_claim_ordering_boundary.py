from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = (
    "src/aigineering/cli/worker.py",
    "src/aigineering/cli/run.py",
    "src/aigineering/cli/_common.py",
    "src/aigineering/local_fleet.py",
    "src/aigineering/agent/engine_worker.py",
    "src/aigineering/methods/service.py",
    "src/aigineering/server/app.py",
)


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_operational_application_claims_use_shared_ordering_boundary():
    for path in ENTRYPOINTS:
        tree = ast.parse(_source(path))
        imported_runtime_claim = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "aigineering.runtime"
            and any(alias.name == "claim_next_package" for alias in node.names)
            for node in ast.walk(tree)
        )
        assert not imported_runtime_claim, path
        if path == "src/aigineering/cli/run.py":
            assert "claim_next_application_package" not in _source(path)
            local_fleet = _source("src/aigineering/local_fleet.py")
            owner = ast.parse(local_fleet)
            calls = [
                node
                for node in ast.walk(owner)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "order_application_contract_ids"
            ]
            assert calls, "local cycle claim owner must preserve shared ordering"
        else:
            assert "claim_next_application_package" in _source(path), path

    run_tree = ast.parse(_source("src/aigineering/cli/run.py"))
    local_cycle_calls = [
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_local_cycle"
    ]
    assert local_cycle_calls, "ordinary --once must delegate to the shared cycle owner"


def test_priority_is_composed_in_application_services_not_runtime_kernel():
    runtime = _source("src/aigineering/runtime.py")
    services = _source("src/aigineering/runtime_services.py")
    assert "task_priority" not in runtime
    assert "order_application_contract_ids" in services
    assert "order_task_contract_ids" in services
