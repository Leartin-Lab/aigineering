"""Bulk priority and retry stay on existing signed application owners."""

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


def test_bulk_commands_delegate_to_existing_application_publication_owners():
    priority_cli = _module("src/aigineering/cli/task_priority.py")
    retry_cli = _module("src/aigineering/cli/task_board.py")
    priority_imports = tuple(_imports(priority_cli))
    retry_imports = tuple(_imports(retry_cli))

    assert "aigineering.task_priority" in priority_imports
    assert "aigineering.task_publication" in retry_imports
    for imports in (priority_imports, retry_imports):
        assert not any(
            name in imports
            for name in (
                "aigineering.core.candidate_publisher",
                "aigineering.core.commitment",
                "aigineering.protocol.candidate",
                "aigineering.protocol.effect_builders",
            )
        )

    priority_batch = ast.unparse(_function(priority_cli, "_task_priority_batch"))
    retry_batch = ast.unparse(_function(retry_cli, "_task_retry_batch"))
    assert "_redis_protected = True" in ast.unparse(priority_cli)
    assert "_redis_protected = True" in ast.unparse(retry_cli)
    assert "publish_task_priority" in priority_batch
    assert "retry_local_task_result" in retry_batch
    assert "batch_correlation_id" in retry_batch
    assert "key_for_target=lambda _contract_id: None" in retry_batch


def test_shared_collector_only_collects_results_and_never_publishes_facts():
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
