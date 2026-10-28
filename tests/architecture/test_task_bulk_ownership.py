"""Bulk CLI collection delegates stateful effects to shared application owners."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _function(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_bulk_collector_contains_no_signing_or_domain_effects():
    path = ROOT / "src/aigineering/cli/_task_batch.py"
    source = path.read_text(encoding="utf-8")
    assert "publish_effect" not in source
    assert "CandidatePublisher" not in source
    assert "CandidateEffect" not in source


def test_task_commands_delegate_cancel_and_task_control_to_application_owners():
    board = ROOT / "src/aigineering/cli/task_board.py"
    cancellation = ast.unparse(_function(board, "task_cancel"))
    assert "publish_task_cancel" in cancellation
    assert "contract_cancellation_effect" not in cancellation
    collab = ROOT / "src/aigineering/cli/task_collaboration.py"
    for name in ("task_block", "task_unblock", "task_promote"):
        body = ast.unparse(_function(collab, name))
        assert "publish_task_" in body
