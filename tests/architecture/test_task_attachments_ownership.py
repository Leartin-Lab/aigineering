"""Attachment association remains a signed plugin fact, not input state."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def test_attachment_application_has_one_candidate_writer_and_no_cli_dependency():
    tree = _tree("src/aigineering/task_attachments.py")
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.startswith("aigineering.cli") for module in imports)
    assert "aigineering.plugins.task_attachments" in imports
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "publish" in calls
    assert not calls.intersection(
        {
            "add_asset",
            "add_contract",
            "append_runtime_record",
            "commit_ingress_batch",
            "commit_candidate",
            "claim_contract",
        }
    )


def test_attachment_fact_is_registered_only_on_existing_task_group():
    plugin = (ROOT / "src/aigineering/plugins/task_attachments.py").read_text()
    task_cli = _tree("src/aigineering/cli/task.py")
    assert '"task.attachment.link"' in plugin
    assert '"task.attachment.linked"' in plugin
    assert "direct_output_fact_types=frozenset({LINKED})" in plugin
    assert "reserved_fact_types=frozenset({LINKED})" in plugin
    for path in (ROOT / "src/aigineering/core").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "task_attachments" not in source
        assert '"task.attachment.link"' not in source
        assert '"task.attachment.linked"' not in source
    matches = [
        node
        for node in ast.walk(task_cli)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_task_attachment_command"
    ]
    assert len(matches) == 1
