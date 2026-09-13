"""Local validation adapters produce Candidates without owning execution state."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_local_validation_adapters_do_not_import_runtime_or_store():
    for relative in (
        "src/aigineering/agent/local_worker.py",
        "src/aigineering/business/claim_check_worker.py",
        "src/aigineering/business/claim_checks.py",
        "src/aigineering/business/claim_review_contracts.py",
        "src/aigineering/business/claim_review_workers.py",
        "src/aigineering/business/claim_review_acceptance.py",
    ):
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            for name in imports:
                assert not any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in (
                        "aigineering.runtime",
                        "aigineering.local_fleet",
                        "aigineering.core.store",
                        "aigineering.core.sqlite_store",
                        "aigineering.core.commitment",
                        "aigineering.core.candidate_publisher",
                    )
                ), (relative, name)
