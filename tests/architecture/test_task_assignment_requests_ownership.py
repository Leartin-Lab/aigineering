"""Assignment request files preserve input context without owning authority."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(function: ast.FunctionDef) -> list[tuple[str, int]]:
    return [
        (node.func.id, node.lineno)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def test_request_application_module_is_read_and_local_file_only():
    tree = _tree("src/aigineering/task_assignment_requests.py")
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "aigineering.core.claim_assignments" in imports
    assert "aigineering.core.domain" in imports
    assert "aigineering.core.projection_context" in imports
    assert "aigineering.protocol.identity" in imports
    assert not any(
        name in imports
        for name in (
            "aigineering.core.candidate_publisher",
            "aigineering.core.commitment",
            "aigineering.local_identity",
            "aigineering.plugins.task_assignment",
            "aigineering.runtime_services",
        )
    )


def test_current_request_is_saved_before_shared_target_publication():
    tree = _tree("src/aigineering/cli/task_assignment.py")
    function = _function(tree, "_current_reassign_request")
    calls = dict(_calls(function))
    assert calls["prepare_assignment_request"] < calls["save_assignment_request"]
    assert calls["save_assignment_request"] < calls["_collect_reassign_targets"]


def test_resume_loads_and_domain_checks_frozen_input_before_publication():
    tree = _tree("src/aigineering/cli/task_assignment.py")
    function = _function(tree, "_resume_reassign_request")
    calls = dict(_calls(function))
    assert calls["load_assignment_request"] < calls["_reassign_preflight"]
    assert calls["_reassign_preflight"] < calls["_collect_reassign_targets"]


def test_cli_keeps_existing_signed_publisher_and_single_batch_collector():
    source = (ROOT / "src/aigineering/cli/task_assignment.py").read_text(
        encoding="utf-8"
    )
    assert "publish_task_assignment(" in source
    assert "collect_task_batch(" in source
    assert "CandidatePublisher" not in source
    assert "publish_effect(" not in source
    assert "SQLiteStore(" not in source
