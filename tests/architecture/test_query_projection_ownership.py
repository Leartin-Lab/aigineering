"""Redis query generations use the shared SQLite snapshot owner."""

from __future__ import annotations

import ast
from pathlib import Path


def test_core_query_projection_uses_read_owners_not_storage_or_redis_adapters():
    module = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "aigineering"
        / "core"
        / "query_projection.py"
    )
    source = module.read_text()
    tree = ast.parse(source)
    forbidden_modules = {
        "aigineering.core.sqlite_store",
        "aigineering.adapters.redis_query",
    }
    assert not any(
        (isinstance(node, ast.ImportFrom) and node.module in forbidden_modules)
        or (
            isinstance(node, ast.Import)
            and any(alias.name in forbidden_modules for alias in node.names)
        )
        for node in tree.body
    )
    forbidden_calls = {"execute", "executemany", "executescript", "backup"}
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_calls
        for node in ast.walk(tree)
    )
