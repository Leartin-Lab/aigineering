"""Operational Worker entry points keep Candidate authentication ownership."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def test_claim_and_renew_writer_apis_accept_original_candidate():
    tree = _tree("src/aigineering/core/sqlite_store.py")
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("claim_contract", "renew_claim"):
        function = functions[name]
        names = {arg.arg for arg in function.args.args + function.args.kwonlyargs}
        assert "claim_candidate" in names


def test_all_operational_claim_next_calls_supply_or_derive_a_candidate():
    files = (
        "src/aigineering/server/app.py",
        "src/aigineering/local_fleet.py",
        "src/aigineering/agent/engine_worker.py",
        "src/aigineering/methods/service.py",
        "src/aigineering/cli/run.py",
        "src/aigineering/cli/_common.py",
        "src/aigineering/cli/worker.py",
    )
    for path in files:
        tree = _tree(path)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "claim_next_application_package"
        ]
        if path == "src/aigineering/cli/run.py":
            assert not calls, "CLI must delegate claim authorization to the cycle owner"
            delegated = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_local_cycle"
            ]
            assert delegated, "CLI --once must call the shared local cycle owner"
            owner = _tree("src/aigineering/local_fleet.py")
            calls = [
                node
                for node in ast.walk(owner)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "claim_next_application_package"
            ]
            ordered = [
                node
                for node in ast.walk(owner)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "order_application_contract_ids"
            ]
            assert calls and ordered, (
                "shared cycle owner must retain ordered signed claims"
            )
        assert calls, f"expected shared application claim call in {path}"
        for call in calls:
            keywords = {item.arg: item.value for item in call.keywords}
            assert "claim_candidate" in keywords, (path, call.lineno)


def test_coordination_auth_core_does_not_import_plugin_implementations():
    tree = _tree("src/aigineering/core/worker_coordination.py")
    imports = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert not any(module.startswith("aigineering.plugins") for module in imports)
