"""The task board owns views and delegates all writes to signed Candidates."""

import ast
from pathlib import Path


SOURCE = Path(__file__).parents[2] / "src/aigineering/cli/task_board.py"
PUBLICATION = Path(__file__).parents[2] / "src/aigineering/task_publication.py"
QUERY = Path(__file__).parents[2] / "src/aigineering/task_view_query.py"
VIEWS = Path(__file__).parents[2] / "src/aigineering/task_views.py"


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


def test_task_board_reads_shared_views_without_owning_runtime_or_storage():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = tuple(_imports(tree))
    assert "aigineering.task_views" in imports
    assert "aigineering.application" in imports
    assert not any(
        token in module
        for module in imports
        for token in (
            "sqlite_store",
            "local_fleet",
            "worker_host",
            "scheduler",
            "queue",
        )
    )
    assert "aigineering.core.sqlite_store" not in imports

    for name in ("task_list", "task_board", "task_stats", "task_show", "task_result"):
        source = ast.unparse(_function(tree, name))
        assert "commit_local_effect" not in source
        assert "publish_effect" not in source


def test_mutating_commands_only_submit_canonical_candidate_effects():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cancel = ast.unparse(_function(tree, "task_cancel"))
    retry = ast.unparse(_function(tree, "task_retry"))
    assert "publish_task_cancel" in cancel
    assert "contract_cancellation_effect" not in cancel
    assert "retry_local_task_result" in retry
    assert "commit_local_effect" not in retry
    assert "contract_declaration_effect" not in retry
    assert "reset" not in cancel.lower()
    assert "reset" not in retry.lower()

    publication_tree = ast.parse(PUBLICATION.read_text(encoding="utf-8"))
    shared_retry = ast.unparse(
        _function(publication_tree, "_retry_local_task_operation")
    )
    assert "commit_local_effect" in shared_retry
    assert "retry:{original.id}:{proposed.id}" in shared_retry
    shared_cancel = ast.unparse(_function(publication_tree, "publish_task_cancel"))
    assert "commit_local_effects" in shared_cancel
    assert "contract_cancellation_effect" in shared_cancel


def test_aggregate_task_views_reuse_one_explicit_read_snapshot():
    board_tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fetch = ast.unparse(_function(board_tree, "_fetch_cards"))
    assert "load_task_view_query_context" in fetch
    assert "load_effect_projection_context" not in fetch
    assert "query_context.revision != revision" in fetch
    assert "supplied_contracts != source_contracts" in fetch
    assert "projection.get_all_contracts()" not in fetch

    query_tree = ast.parse(QUERY.read_text(encoding="utf-8"))
    query_imports = tuple(_imports(query_tree))
    assert not any("sqlite_store" in module for module in query_imports)
    assert not any("StoreProtocol" in module for module in query_imports)
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__getattr__"
        for node in query_tree.body
    )
    query_source = QUERY.read_text(encoding="utf-8")
    assert "store.scan_runtime_records()" in query_source
    assert "enumerate(" not in query_source
    assert "runtime_records=runtime_records" in query_source

    views_tree = ast.parse(VIEWS.read_text(encoding="utf-8"))
    status = ast.unparse(_function(views_tree, "project_task_status"))
    assert status.index("require_operational_store(store)") < status.index(
        "query_context.require_match"
    )
    assert "check_store_integrity(original_store)" in status
    assert (
        "_descendant_failure_risks(contract, store, projection_context=projection_context)"
        in status
    )
    descendant_risks = ast.unparse(_function(views_tree, "_descendant_failure_risks"))
    assert (
        "_submitted_independent_output_assets(child, store, projection_context)"
        in descendant_risks
    )
