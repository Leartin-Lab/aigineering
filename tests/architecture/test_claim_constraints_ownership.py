from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/aigineering"


def test_prerequisite_semantics_have_one_generic_core_owner():
    owner = (SRC / "core/claim_constraints.py").read_text()
    store = (SRC / "core/sqlite_store.py").read_text()
    projection = (SRC / "core/runtime_projection.py").read_text()
    assert "def open_claim_constraints(" in owner
    assert "def validate_claim_constraint_transition(" in owner
    assert "def validate_claim_constraint_batch(" in owner
    assert "def external_prerequisite_blockers(" in owner
    assert "from aigineering.plugins" not in owner
    assert "claim_constraint_blockers(" in projection
    assert "validate_claim_constraint_batch(" in store
    assert "BEGIN IMMEDIATE" in store[store.index("def claim_contract(") :]
    assert "claim_constraint_blockers(" in store[store.index("def claim_contract(") :]


def test_constraint_facts_require_candidate_ingress_and_reader_floor():
    store = (SRC / "core/sqlite_store.py").read_text()
    memory = (SRC / "core/store.py").read_text()
    migration = (SRC / "core/sqlite_migrations.py").read_text()
    assert '"claim.constraint.placed"' in store
    assert '"claim.constraint.released"' in store
    assert "claim constraint facts require Candidate ingress commitment" in memory
    assert "CURRENT_SCHEMA_VERSION = 21" in migration
    assert "def _migrate_to_v20(self)" in migration
    assert "pre-existing claim constraint facts" in migration


def test_prerequisite_ids_are_explicit_session_references():
    session = (SRC / "core/session.py").read_text()
    assert '"prerequisite_contract_id"' in session
    assert "from aigineering.plugins" not in session
