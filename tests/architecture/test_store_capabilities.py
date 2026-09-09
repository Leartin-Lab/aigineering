import ast
from pathlib import Path

from aigineering.core.store_capabilities import (
    AssetReader,
    CandidateCommitStore,
    ContractReader,
    RuntimeRecordReader,
    WorkerClaimStore,
)

ROOT = Path(__file__).parents[2]


def test_store_capabilities_separate_reads_from_authoritative_writes():
    assert "commit_ingress_batch" not in AssetReader.__dict__
    assert "commit_ingress_batch" not in ContractReader.__dict__
    assert "commit_ingress_batch" not in RuntimeRecordReader.__dict__
    assert "commit_ingress_batch" in CandidateCommitStore.__dict__
    assert "claim_contract" in WorkerClaimStore.__dict__


def test_plugins_do_not_import_candidate_commit_capability():
    for path in (ROOT / "src/aigineering/plugins").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "CandidateCommitStore" not in imported, path
        assert "StoreProtocol" not in imported, path
        assert "commit_ingress_batch" not in path.read_text(encoding="utf-8"), path
