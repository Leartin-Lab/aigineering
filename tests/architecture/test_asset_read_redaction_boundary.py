"""Public Asset inspection shares the canonical disclosure projection."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _function(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def test_cli_and_http_asset_reads_use_the_canonical_redaction_owner():
    cli_source = (ROOT / "src/aigineering/cli/asset.py").read_text()
    server_source = (ROOT / "src/aigineering/server/app.py").read_text()

    cli_function = ast.unparse(_function(cli_source, "asset_show"))
    http_function = ast.unparse(_function(server_source, "_asset_response"))

    assert "from aigineering.core.disclosure import redact_for_disclosure" in cli_source
    assert "redact_for_disclosure(asset)" in cli_function
    assert "disclosure_view" in cli_function
    assert "asset.disclosure_view" in cli_function

    assert (
        "from aigineering.core.disclosure import redact_for_disclosure" in server_source
    )
    assert "redact_for_disclosure(asset)" in http_function
    assert "disclosure_view=asset.disclosure_view" in http_function
