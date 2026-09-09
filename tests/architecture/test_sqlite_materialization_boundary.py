import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATERIALIZATION = ROOT / "src/aigineering/core/sqlite_materialization.py"
STORE = ROOT / "src/aigineering/core/sqlite_store.py"


def test_sqlite_materialization_is_pure_and_connection_free():
    source = MATERIALIZATION.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "sqlite3" not in imported_modules
    assert called_attributes.isdisjoint({"commit", "execute", "executemany"})
    assert all(
        not isinstance(node, (ast.With, ast.AsyncWith)) for node in ast.walk(tree)
    )


def test_sqlite_store_delegates_protocol_row_materialization():
    source = STORE.read_text(encoding="utf-8")

    for helper in (
        "runtime_record_from_row",
        "asset_from_row",
        "contract_from_row",
        "replacement_claim_from_row",
    ):
        assert f"staticmethod({helper})" in source
