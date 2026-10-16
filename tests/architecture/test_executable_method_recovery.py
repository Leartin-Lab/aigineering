"""Method recovery stays on existing Candidate and diagnostic owners."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _imports(tree: ast.AST) -> tuple[str, ...]:
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.append(node.module or "")
    return tuple(result)


def test_recovery_projects_signed_effects_without_owning_storage_or_scheduling():
    path = ROOT / "src/aigineering/plugins/recovery.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = _imports(tree)
    assert "aigineering.protocol.effect_builders" in imports
    assert not any(
        module == "aigineering.core.sqlite_store"
        or module.startswith("aigineering.core.sqlite_store.")
        for module in imports
    )
    assert "runtime.publish_task_effects(" in source
    assert "commit_ingress_batch" not in source
    assert "append_runtime_record" not in source


def test_method_recovery_lineage_reuses_exact_claim_diagnostic_and_shared_history():
    path = ROOT / "src/aigineering/task_lineage.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = _imports(tree)
    assert "aigineering.task_diagnostics" in imports
    assert "aigineering.core.publication_history" in imports
    assert "commit_ingress_batch" not in source
    assert "append_runtime_record" not in source
