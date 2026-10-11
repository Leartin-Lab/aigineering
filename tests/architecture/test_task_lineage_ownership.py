"""Verified lineage remains a read-only application projection."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]
APP = ROOT / "src/aigineering/task_lineage.py"
CLI = ROOT / "src/aigineering/cli/task_lineage.py"
DIAGNOSTICS = ROOT / "src/aigineering/task_diagnostics.py"
PUBLICATION = ROOT / "src/aigineering/task_publication.py"
HANDOFF = ROOT / "src/aigineering/plugins/handoff.py"
REVISION = ROOT / "src/aigineering/plugins/task_revision.py"
SUCCESSOR_PUBLICATIONS = ROOT / "src/aigineering/plugins/successor_publications.py"
PUBLICATION_HISTORY = ROOT / "src/aigineering/core/publication_history.py"
TASK_LINEAGE = ROOT / "src/aigineering/task_lineage.py"


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_lineage_proof_is_shared_and_cli_only_renders_it():
    app_tree = ast.parse(APP.read_text(encoding="utf-8"))
    app_imports = tuple(_imports(app_tree))
    assert "aigineering.task_view_query" in app_imports
    assert "aigineering.core.publication_history" in app_imports
    assert "aigineering.task_diagnostics" in app_imports
    assert "aigineering.plugins.handoff" in app_imports
    assert "aigineering.plugins.task_revision" in app_imports
    assert not any(
        any(
            module == blocked or module.startswith(blocked + ".")
            for blocked in (
                "aigineering.core.sqlite_store",
                "aigineering.core.candidate_publisher",
                "aigineering.runtime",
            )
        )
        for module in app_imports
    )
    app_source = APP.read_text(encoding="utf-8")
    assert "validate_published_member" in app_source
    assert "task_invocation_failure_binding" in app_source
    assert "validate_retry_publication_relations" in app_source
    assert "validate_task_handoff_relations" in app_source
    assert "validate_task_revision_relations" in app_source
    assert "commit_ingress_batch" not in app_source
    assert "append_runtime_record" not in app_source
    assert "task_lineage" in app_source
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "index_published_records"
            for node in ast.walk(app_tree)
        )
        == 1
    )

    publication_tree = ast.parse(PUBLICATION.read_text(encoding="utf-8"))
    publication_imports = tuple(_imports(publication_tree))
    assert "aigineering.core.publication_history" in publication_imports
    publication_source = PUBLICATION.read_text(encoding="utf-8")
    validator = next(
        node
        for node in publication_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "validate_retry_publication_relations"
    )
    assert [arg.arg for arg in validator.args.args] == [
        "query_context",
        "records",
        "publication_index",
    ]
    assert "validate_published_member" in publication_source
    assert "load_task_view_query_context" not in publication_source
    assert "SQLiteStore" not in publication_source
    assert "index_published_records" not in ast.unparse(validator)

    handoff_tree = ast.parse(HANDOFF.read_text(encoding="utf-8"))
    handoff_imports = tuple(_imports(handoff_tree))
    assert "aigineering.plugins.successor_publications" in handoff_imports
    handoff_validator = next(
        node
        for node in handoff_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "validate_task_handoff_relations"
    )
    handoff_validator_source = ast.unparse(handoff_validator)
    assert "validate_successor_publications" in handoff_validator_source
    assert "load_task_view_query_context" not in handoff_validator_source
    assert "SQLiteStore" not in handoff_validator_source
    assert "project_task_handoff_effect" not in handoff_validator_source
    assert "inventory_handoff_tree" not in handoff_validator_source

    revision_tree = ast.parse(REVISION.read_text(encoding="utf-8"))
    revision_validator = next(
        node
        for node in revision_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "validate_task_revision_relations"
    )
    revision_source = ast.unparse(revision_validator)
    assert "validate_successor_publications" in revision_source
    assert "task_revision_records" not in revision_source

    common_tree = ast.parse(SUCCESSOR_PUBLICATIONS.read_text(encoding="utf-8"))
    common_imports = tuple(_imports(common_tree))
    assert "aigineering.core.publication_history" in common_imports
    assert not any(
        module.startswith("aigineering.core.sqlite_store") for module in common_imports
    )
    common_source = SUCCESSOR_PUBLICATIONS.read_text(encoding="utf-8")
    assert "validate_published_member" in common_source
    assert "read_effect_types" in common_source
    assert "contract_to_dict" in common_source

    cli_tree = ast.parse(CLI.read_text(encoding="utf-8"))
    cli_imports = tuple(_imports(cli_tree))
    assert "aigineering.task_lineage" in cli_imports
    assert "aigineering.plugins" not in cli_imports
    assert "aigineering.core" not in cli_imports


def test_claim_diagnostics_reuses_optional_immutable_query_context():
    source = DIAGNOSTICS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    binding = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "task_invocation_failure_binding"
    )
    arguments = {arg.arg for arg in binding.args.kwonlyargs}
    assert "query_context" in arguments
    assert "_select_proven_claim_run" in ast.unparse(binding)
    assert "_snapshot" in source
    assert "query_context.require_match(store)" in source


def test_read_only_effect_container_checks_share_the_neutral_reader():
    for path in (PUBLICATION, TASK_LINEAGE, SUCCESSOR_PUBLICATIONS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert "aigineering.core.publication_history" in tuple(_imports(tree))
        assert "read_effect_types" in path.read_text(encoding="utf-8")

    owner = ast.parse(PUBLICATION_HISTORY.read_text(encoding="utf-8"))
    helper = next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == "read_effect_types"
    )
    helper_source = ast.unparse(helper)
    assert "validate_published_member" not in helper_source
    assert "tuple(effect_types)" in helper_source

    # Read-only feature validators cannot retain independent coercions.
    read_functions = (
        (PUBLICATION, "validate_retry_publication_relations"),
        (TASK_LINEAGE, "task_lineage"),
        (SUCCESSOR_PUBLICATIONS, "validate_successor_publications"),
    )
    for path, function_name in read_functions:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        source = ast.unparse(function)
        assert "read_effect_types" in source
        assert 'tuple(receipt.payload.get("effect_types"' not in source

    for path, function_name in (
        (HANDOFF, "validate_task_handoff_relations"),
        (REVISION, "validate_task_revision_relations"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        assert "validate_successor_publications" in ast.unparse(function)


def test_publication_history_is_neutral_and_attachments_keep_compatibility_shims():
    history = ROOT / "src/aigineering/core/publication_history.py"
    tree = ast.parse(history.read_text(encoding="utf-8"))
    imports = tuple(_imports(tree))
    assert not any(module.startswith("aigineering.plugins") for module in imports)
    assert "aigineering.protocol.runtime_record" in imports
    plugin = ROOT / "src/aigineering/plugins/task_attachments.py"
    plugin_source = plugin.read_text(encoding="utf-8")
    assert "from aigineering.core.publication_history import (" in plugin_source
    assert "validate_published_member" in plugin_source
