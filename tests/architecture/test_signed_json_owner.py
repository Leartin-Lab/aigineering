"""Keep the signed JSON boundary at the protocol owner."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_asset_graph_reuses_candidate_signed_json_validation():
    path = ROOT / "src/aigineering/protocol/asset_graph.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "aigineering.protocol.candidate"
        for alias in node.names
    }
    assert "_validate_signed_json" in imports


def test_business_acceptance_does_not_import_signed_json_subset():
    path = ROOT / "src/aigineering/core/acceptance.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "_validate_signed_json" not in imports
