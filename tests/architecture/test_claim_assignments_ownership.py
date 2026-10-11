from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_assignment_semantics_have_one_core_owner_and_no_plugin_imports():
    owner = (ROOT / "src/aigineering/core/claim_assignments.py").read_text()
    projector = (ROOT / "src/aigineering/core/effect_projection.py").read_text()
    assert "plugins." not in owner
    assert "plugins." not in projector
    assert "def validate_assignment_batch" in owner
    assert "def assignment_history" in owner
    assert "def assignment_blocker" in owner


def test_assignment_writer_is_candidate_bound_and_rechecked_in_sqlite():
    sqlite = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()
    memory = (ROOT / "src/aigineering/core/store.py").read_text()
    assert '"claim.assignment.changed"' in sqlite
    assert "validate_assignment_batch(" in sqlite
    assert "candidate_proposal" in sqlite
    assert "validate_snapshot" in sqlite
    assert "allow_claim_assignment_fact=True" in sqlite
    assert "assignment facts require Candidate ingress commitment" in sqlite
    assert "validate_assignment_batch(" in memory
    assert "assignment facts require their signed Candidate projection" in memory


def test_assignment_fact_reader_floor_and_shared_projection_store_view():
    migrations = (ROOT / "src/aigineering/core/sqlite_migrations.py").read_text()
    view = (ROOT / "src/aigineering/core/projection_store_view.py").read_text()
    successor = (ROOT / "src/aigineering/plugins/task_successors.py").read_text()
    assert "CURRENT_SCHEMA_VERSION = 21" in migrations
    assert "_migrate_to_v21" in migrations
    assert "claim.assignment.changed" in migrations
    assert "class ProjectionStoreView" in view
    assert (
        "from aigineering.core.projection_store_view import ProjectionStoreView"
        in successor
    )


def test_worker_references_use_one_typed_session_owner():
    session = (ROOT / "src/aigineering/core/session.py").read_text()
    assert "def runtime_record_worker_references" in session
    assert '"claim.assignment.changed"' in session
    assert "mentions worker" not in session.lower()
