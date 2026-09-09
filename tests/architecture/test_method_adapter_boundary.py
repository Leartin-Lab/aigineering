"""Methods remain adapters over the canonical runtime, not a second kernel."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "aigineering"


def test_method_adapters_have_no_direct_fact_write_or_code_execution_path():
    forbidden = {
        "commit_ingress_batch",
        "append_runtime_record",
        "append_trace_entry",
        "set_idempotency",
        "_insert_asset",
        "_insert_contract",
        "exec",
        "eval",
    }
    for path in (ROOT / "methods").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                assert name not in forbidden, (path.name, node.lineno, name)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("aigineering.cli"), path


def test_kernel_and_protocol_do_not_import_method_package_semantics():
    for directory in ("core", "protocol"):
        for path in (ROOT / directory).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("aigineering.methods"), (
                        path
                    )
                elif isinstance(node, ast.Import):
                    assert not any(
                        alias.name.startswith("aigineering.methods")
                        for alias in node.names
                    ), path
