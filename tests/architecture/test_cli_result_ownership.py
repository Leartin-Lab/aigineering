"""Task command result formatting has one CLI-only owner."""

import ast
import importlib
from pathlib import Path


ROOT = Path(__file__).parents[2]

_ALIASES = {
    "task_assignment": {
        "_failure": "_cli_result_failure",
        "_success": "_cli_result_success",
        "_pending_failure": "_cli_pending_failure",
        "_internal_error": "_cli_internal_error",
    },
    "task_collaboration": {
        "_failure": "_cli_result_failure",
        "_success": "_cli_result_success",
    },
    "task_dependencies": {
        "_failure": "_cli_result_failure",
        "_success": "_cli_result_success",
    },
    "task_revision": {
        "_failure": "_cli_result_failure",
        "_success": "_cli_result_success",
    },
    "task_handoff": {"_success": "_cli_result_success"},
    "task_priority": {
        "_error": "_cli_result_failure",
        "_success": "_cli_result_success",
        "_pending_failure": "_cli_pending_failure",
        "_internal_error": "_cli_internal_error",
    },
    "task": {"_parse_json_object": "_cli_json_object_option"},
    "contract": {"_parse_json_object": "_cli_json_object_option"},
}


def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_task_cli_modules_import_their_shared_result_owner():
    common = importlib.import_module("aigineering.cli._common")
    for module_name, aliases in _ALIASES.items():
        module = importlib.import_module(f"aigineering.cli.{module_name}")
        for local_name, common_name in aliases.items():
            assert getattr(module, local_name) is getattr(common, common_name)


def test_shared_result_helpers_do_not_acquire_or_publish_runtime_state():
    tree = _tree("src/aigineering/cli/_common.py")
    helper_names = {
        common_name for aliases in _ALIASES.values() for common_name in aliases.values()
    }
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in helper_names
    ]
    assert {helper.name for helper in helpers} == helper_names

    forbidden = {
        "CandidatePublisher",
        "Store",
        "SQLiteStore",
        "commit_local_effect",
        "commit_local_effects",
        "ensure_local_domain",
        "persistent_snapshot",
        "persistent_store",
        "publish",
        "sign",
    }
    for helper in helpers:
        names = {node.id for node in ast.walk(helper) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(helper) if isinstance(node, ast.Attribute)
        }
        assert names.isdisjoint(forbidden), helper.name


def test_semantically_extended_renderers_keep_their_local_owner():
    handoff = _tree("src/aigineering/cli/task_handoff.py")
    handoff_failure = _function(handoff, "_failure")
    assert [argument.arg for argument in handoff_failure.args.kwonlyargs] == [
        "next_step"
    ]

    collaboration = importlib.import_module("aigineering.cli.task_collaboration")
    common = importlib.import_module("aigineering.cli._common")
    assert collaboration._pending_failure is not common._cli_pending_failure
