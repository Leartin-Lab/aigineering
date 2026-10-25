"""Private publication relations stay a pure request plus SQLite adapter."""

from __future__ import annotations

import ast
from pathlib import Path


def _package() -> Path:
    return Path(__file__).resolve().parents[2] / "src" / "aigineering"


def test_publication_relation_storage_has_only_sqlite_schema_and_store_owners():
    package = _package()
    owners = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "publication_head_members" in path.read_text()
        or "publication_index_state" in path.read_text()
    }

    assert owners == {"core/sqlite_schema.py", "core/sqlite_store.py"}


def test_pure_relation_requests_do_not_import_or_call_store_or_sql():
    package = _package()
    source = package / "core" / "allowance_publication_index.py"
    tree = ast.parse(source.read_text())

    assert not any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith(("sqlite_store", "store"))
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany", "commit", "rollback"}
        for node in ast.walk(tree)
    )


def test_publication_selector_is_read_only_and_does_not_repair_cache():
    package = _package()
    source = package / "core" / "sqlite_store.py"
    tree = ast.parse(source.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SQLiteStore"
    )
    selector = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "read_allowance_publication_selection"
    )

    assert "rebuild_publication_index" not in {
        node.id for node in ast.walk(selector) if isinstance(node, ast.Name)
    }
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback", "executemany"}
        for node in ast.walk(selector)
    )
