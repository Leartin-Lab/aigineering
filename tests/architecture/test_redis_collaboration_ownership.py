"""Keep Redis collaboration as an application projection, outside kernel authority."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_redis_collaboration_has_no_core_redis_dependency_or_write_authority():
    service = ROOT / "src/aigineering/redis_collaboration.py"
    imports = _imports(service)
    assert not any(
        name == "aigineering.core.sqlite_store"
        or name == "sqlite3"
        or name.startswith("aigineering.core.commitment")
        or name.startswith("aigineering.core.authority")
        for name in imports
    )
    assert "redis" not in imports
    source = service.read_text(encoding="utf-8")
    for forbidden in (
        ".append_runtime_record(",
        ".commit_ingress_batch(",
        ".add_asset(",
        ".add_contract(",
    ):
        assert forbidden not in source


def test_redis_adapter_is_only_composed_at_application_boundary():
    application = _imports(ROOT / "src/aigineering/application.py")
    assert "aigineering.redis_collaboration" in application
    core = ROOT / "src/aigineering/core"
    for path in core.rglob("*.py"):
        assert not any("redis" in module.lower() for module in _imports(path)), path
