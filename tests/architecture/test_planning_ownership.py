"""Ownership constraints for Plugin-native planning semantics."""

from __future__ import annotations

import ast
from pathlib import Path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def test_plan_scaffold_is_plugin_owned_and_store_free():
    assert not Path("src/aigineering/core/plan_scaffold.py").exists()
    owner = Path("src/aigineering/plugins/plan_scaffold.py")
    imports = _imports(owner)
    forbidden = {
        "aigineering.core.sqlite_store",
        "aigineering.core.store",
        "aigineering.runtime",
    }
    assert imports.isdisjoint(forbidden)


def test_task_semantics_uses_one_planning_reserved_prefix_owner():
    source = Path("src/aigineering/plugins/task_semantics.py").read_text()
    assert "from aigineering.plugins.plan_scaffold import" in source
    assert "PLAN_RESERVED_PREFIXES" in source
    assert "RESERVED_PREFIXES |" not in source
