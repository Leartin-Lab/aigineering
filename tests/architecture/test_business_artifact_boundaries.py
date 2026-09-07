"""The business artifact adapters remain pure over the runtime boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "aigineering"


def _imports(path: Path) -> set[str]:
    result: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _is_or_below(value: str, prefix: str) -> bool:
    return value == prefix or value.startswith(prefix + ".")


def test_core_and_protocol_do_not_depend_on_business_or_artifact_cli() -> None:
    violations: list[str] = []
    for root, module_prefix in (
        (SRC / "core", "aigineering.core"),
        (SRC / "protocol", "aigineering.protocol"),
    ):
        for path in sorted(root.rglob("*.py")):
            for imported in _imports(path):
                if _is_or_below(imported, "aigineering.business") or imported in {
                    "aigineering.cli.artifact"
                }:
                    violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert violations == []


def test_business_adapters_do_not_write_runtime_state_directly() -> None:
    forbidden_imports = {
        "aigineering.core.store",
        "aigineering.core.sqlite_store",
        "aigineering.core.commitment",
    }
    forbidden_calls = {"add_asset", "add_runtime_record", "commit"}
    violations: list[str] = []
    business_root = SRC / "business"
    for path in sorted(business_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imports(path):
            if imported in forbidden_imports or any(
                _is_or_below(imported, prefix) for prefix in forbidden_imports
            ):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden_calls:
                    violations.append(
                        f"{path.relative_to(ROOT)} calls {node.func.attr}"
                    )
    assert violations == []
