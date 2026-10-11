"""Cancellation result classification stays in the existing owners."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    source = ROOT / path
    return ast.parse(source.read_text(encoding="utf-8"), filename=str(source))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def test_application_owns_only_verified_durable_cancel_rejection_results():
    tree = _tree("src/aigineering/task_publication.py")
    exception = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CancelPublicationRejected"
    )
    bases = {base.id for base in exception.bases if isinstance(base, ast.Name)}
    assert "ValueError" in bases
    publisher = _function(tree, "publish_task_cancel")
    raises = [
        node
        for node in ast.walk(publisher)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "CancelPublicationRejected"
    ]
    assert len(raises) == 2
    assert "_cancel_rejection_result" in _called_names(publisher)
    assert "verify_durable_rejection_member" in _called_names(
        _function(tree, "_cancel_rejection_result")
    )
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        module == "aigineering.cli" or module.startswith("aigineering.cli.")
        for module in imported
    )


def test_cancel_cli_classifies_typed_rejection_before_untyped_value_error():
    tree = _tree("src/aigineering/cli/task_board.py")
    cancel = _function(tree, "task_cancel")
    relevant: list[str] | None = None
    for node in ast.walk(cancel):
        if isinstance(node, ast.Try):
            current: list[str] = []
            for handler in node.handlers:
                if isinstance(handler.type, ast.Name):
                    current.append(handler.type.id)
            if {"CancelPublicationRejected", "ValueError"} <= set(current):
                relevant = current
                break
    assert relevant is not None
    assert relevant.index("CancelPublicationRejected") < relevant.index("ValueError")
    calls = [node for node in ast.walk(cancel) if isinstance(node, ast.Call)]
    bulk = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "collect_task_batch"
    )
    keywords = {item.arg: item.value for item in bulk.keywords}
    expected = keywords["expected_rejection_types"]
    assert isinstance(expected, ast.Tuple)
    assert len(expected.elts) == 1
    assert isinstance(expected.elts[0], ast.Name)
    assert expected.elts[0].id == "CancelPublicationRejected"
    assert "rejection_result" in keywords
    assert isinstance(keywords.get("include_operation_revision"), ast.Constant)
    assert keywords["include_operation_revision"].value is True


def test_generic_batch_collector_is_not_given_cancel_specific_policy():
    tree = _tree("src/aigineering/cli/_task_batch.py")
    collector = _function(tree, "collect_task_batch")
    # The collector remains policy-neutral: cancellation opts into a typed
    # result through its existing callback instead of changing generic rules.
    assert "CancelPublicationRejected" not in ast.unparse(collector)
