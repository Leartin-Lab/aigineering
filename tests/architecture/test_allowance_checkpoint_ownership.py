"""Allowance checkpoints remain private, rebuildable SQLite projections."""

from __future__ import annotations

import ast
from pathlib import Path


def _repo_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[2]
    return root / "src" / "aigineering", root / "tests"


def test_private_checkpoint_table_has_only_sqlite_adapter_owners():
    package, _ = _repo_paths()
    owners = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "allowance_checkpoints" in path.read_text()
    }

    assert owners == {"core/sqlite_schema.py", "core/sqlite_store.py"}


def test_pure_raw_fold_has_no_store_or_sql_access():
    package, _ = _repo_paths()
    source = package / "core" / "causal_allowance.py"
    tree = ast.parse(source.read_text())
    fold = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "fold_raw_allowance_records"
    )

    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany", "commit", "rollback"}
        for node in ast.walk(fold)
    )
    assert not any(
        isinstance(node, ast.Name) and node.id in {"SQLiteStore", "StoreProtocol"}
        for node in ast.walk(fold)
    )


def test_checkpoint_has_no_new_schema_floor_or_semantic_writer_consumer():
    package, _ = _repo_paths()
    schema = (package / "core" / "sqlite_schema.py").read_text()
    store = (package / "core" / "sqlite_store.py").read_text()
    protocol = (package / "core" / "store.py").read_text()

    assert "DDL_CREATE_ALLOWANCE_CHECKPOINTS" in schema
    assert "allowance_checkpoint_mode" in store
    assert "def read_raw_allowance_checkpoint" in store
    assert "def rebuild_allowance_checkpoints" in store
    assert "def audit_allowance_checkpoints" in store
    assert "allowance_checkpoints" not in protocol
    assert "read_raw_allowance_checkpoint" not in protocol
    assert "allowance_available(" not in store
