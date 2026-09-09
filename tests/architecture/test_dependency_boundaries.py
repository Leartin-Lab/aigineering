"""AST checks for the kernel's one-way dependency boundaries."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest


ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = ROOT / "src" / "aigineering" / "core"

_FORBIDDEN_PREFIXES = (
    "aigineering.plugins",
    "aigineering.cli",
    "aigineering.agent",
    "aigineering.server",
    "aigineering.diagnostics",
    "aigineering.application",
    "aigineering.local_fleet",
    "aigineering.fleet_config",
    "aigineering.local_identity",
    "aigineering.worker_hosting",
)

# These are source-compatibility shims with deliberately exact owners and
# targets.  They are kept separate from the forbidden list so a new core
# module cannot inherit an accidental exemption.
_ALLOWED_COMPATIBILITY_IMPORTS = {
    "methods.py": frozenset({"aigineering.plugins.task_semantics"}),
    "provider_config.py": frozenset({"aigineering.fleet_config"}),
}


def _resolve_from_import(module: str, node: ast.ImportFrom, imported: str) -> str:
    """Resolve an ``ImportFrom`` node to the imported qualified name."""

    if node.level == 0:
        base_parts = node.module.split(".") if node.module else []
    else:
        package_parts = module.split(".")[:-1]
        if node.level > len(package_parts):
            raise ValueError(
                f"relative import climbs above package: {module!r}, level={node.level}"
            )
        base_parts = package_parts[: len(package_parts) - (node.level - 1)]
        if node.module:
            base_parts.extend(node.module.split("."))

    if imported == "*":
        return ".".join(base_parts)
    return ".".join((*base_parts, imported))


def collect_imports(source: str, *, module: str) -> frozenset[str]:
    """Collect qualified imports from Python syntax, resolving relatives.

    ``import x as y`` contributes ``x``.  ``from x import y as z`` contributes
    ``x.y`` so package-level imports such as ``from aigineering import
    local_fleet`` remain visible to the boundary check.
    """

    imports: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(
                _resolve_from_import(module, node, alias.name) for alias in node.names
            )
    return frozenset(imports)


def _is_forbidden(import_name: str) -> bool:
    return any(
        import_name == prefix or import_name.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_PREFIXES
    )


@pytest.mark.parametrize(
    ("source", "module", "expected"),
    [
        (
            "import aigineering.application as app",
            "aigineering.core.runtime_projection",
            {"aigineering.application"},
        ),
        (
            "from aigineering import local_fleet as fleet",
            "aigineering.core.runtime_projection",
            {"aigineering.local_fleet"},
        ),
        (
            dedent(
                """
                from aigineering.plugins import (
                    task_semantics as semantics,
                )
                """
            ),
            "aigineering.core.methods",
            {"aigineering.plugins.task_semantics"},
        ),
        (
            "from .. import worker_hosting as hosting",
            "aigineering.core.runtime_projection",
            {"aigineering.worker_hosting"},
        ),
        (
            dedent(
                """
                # import aigineering.agent.worker
                text = "from aigineering.cli import task"
                """
            ),
            "aigineering.core.runtime_projection",
            set(),
        ),
    ],
)
def test_collect_imports_is_ast_based_and_resolves_aliases_and_relatives(
    source: str, module: str, expected: set[str]
) -> None:
    assert collect_imports(source, module=module) == frozenset(expected)


def test_core_dependency_boundaries_have_only_documented_compatibility_shims() -> None:
    violations: list[str] = []
    for path in sorted(CORE_ROOT.rglob("*.py")):
        relative = path.relative_to(CORE_ROOT)
        module = "aigineering.core." + ".".join(relative.with_suffix("").parts)
        imports = collect_imports(path.read_text(encoding="utf-8"), module=module)
        allowed = _ALLOWED_COMPATIBILITY_IMPORTS.get(relative.as_posix(), frozenset())
        for imported in sorted(imports):
            if not _is_forbidden(imported):
                continue
            if any(
                imported == target or imported.startswith(f"{target}.")
                for target in allowed
            ):
                continue
            violations.append(f"{path.relative_to(ROOT)} imports {imported}")

    assert violations == []
