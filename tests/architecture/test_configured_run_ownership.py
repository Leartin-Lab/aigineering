"""Keep configured-run ownership in the application layer."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_configured_run_service_does_not_depend_on_cli_or_write_store_directly():
    source = ROOT / "src/aigineering/configured_run.py"
    imports = _imports(source)
    assert not any(
        name == "aigineering.cli" or name.startswith("aigineering.cli.")
        for name in imports
    )
    assert "aigineering.core.sqlite_store" not in imports
    assert "sqlite3" not in imports
    for owner in ("configured_run.py", "run_config.py", "local_publication.py"):
        body = (ROOT / "src/aigineering" / owner).read_text(encoding="utf-8")
        for forbidden in (
            ".add_asset(",
            ".add_contract(",
            ".append_runtime_record(",
            ".commit_ingress_batch(",
        ):
            assert forbidden not in body
    parser_imports = _imports(ROOT / "src/aigineering/run_config.py")
    assert not any(
        name == "aigineering.cli" or name.startswith("aigineering.cli.")
        for name in parser_imports
    )


def test_run_and_fleet_cli_delegate_to_shared_configured_run_owner():
    run_source = (ROOT / "src/aigineering/cli/run.py").read_text(encoding="utf-8")
    fleet_source = (ROOT / "src/aigineering/cli/fleet.py").read_text(encoding="utf-8")
    assert "aigineering.configured_run" in run_source and "run_configured" in run_source
    assert (
        "aigineering.run_config" in run_source
        and "prepare_configured_run" in run_source
    )
    assert (
        "aigineering.configured_run" in fleet_source
        and "run_configured" in fleet_source
    )
    assert (
        "aigineering.run_config" in fleet_source
        and "prepare_configured_run" in fleet_source
    )
    assert "run_local_fleet(" not in fleet_source
    assert "ensure_local_worker_host(" not in fleet_source
