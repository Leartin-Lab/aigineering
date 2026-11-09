"""Task retry remains on the ordinary signed publication owner."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module(path: str):
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def test_retry_command_and_service_delegate_to_the_core_publication_owner():
    board = _module("src/aigineering/cli/task_board.py")
    publication = _module("src/aigineering/task_publication.py")
    retry = ast.unparse(_function(board, "task_retry"))
    batch = ast.unparse(_function(board, "_task_retry_batch"))
    assert "retry_local_task_result" in retry or "_task_retry_batch" in retry
    assert "retry_local_task_result" in batch
    assert "publish_task_priority" not in retry + batch
    assert "publish_task_priority" not in "\n".join(_imports(board))
    assert "commit_local_effect" in ast.unparse(
        _function(publication, "_retry_local_task_operation")
    )


def test_shared_batch_collector_only_collects_results():
    collector = _module("src/aigineering/cli/_task_batch.py")
    imports = tuple(_imports(collector))
    assert not any(
        name in imports
        for name in (
            "aigineering.task_priority",
            "aigineering.task_publication",
            "aigineering.core.candidate_publisher",
            "aigineering.core.commitment",
        )
    )
    source = ast.unparse(_function(collector, "collect_task_batch"))
    assert "invoke(contract_id, key)" in source
    assert "commit_local_effect" not in source
    assert "CandidatePublisher" not in source
