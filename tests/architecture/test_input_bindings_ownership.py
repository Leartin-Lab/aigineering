from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_exact_input_selection_has_one_core_owner_for_disclosure_and_verification():
    bindings = (ROOT / "src/aigineering/core/input_bindings.py").read_text()
    disclosure = (ROOT / "src/aigineering/core/disclosure.py").read_text()
    verification = (ROOT / "src/aigineering/core/verification.py").read_text()
    admission = (ROOT / "src/aigineering/core/contract_admission.py").read_text()
    owners = "\n".join((bindings, disclosure, verification, admission))

    assert "def read_contract_input_assets(" in bindings
    assert "read_contract_input_assets(contract, store)" in disclosure
    assert "read_contract_input_assets(contract, store)" in verification
    assert "validate_input_asset_binding_targets" in admission
    assert "from aigineering.plugins" not in owners


def test_v6_marker_and_schema_version_guard_binding_readers():
    identity = (ROOT / "src/aigineering/protocol/identity.py").read_text()
    migrations = (ROOT / "src/aigineering/core/sqlite_migrations.py").read_text()
    assert "def hash_contract_v6(" in identity
    assert "hash_contract_v5(" in identity
    assert '"input_asset_bindings"' in identity
    assert "CURRENT_SCHEMA_VERSION = 21" in migrations
    assert "def _migrate_to_v18(self)" in migrations
    assert "CREATE TABLE input_asset_bindings" not in migrations


def test_task_revision_semantics_remain_in_plugin_and_reuse_successor_owner():
    revision = (ROOT / "src/aigineering/plugins/task_revision.py").read_text()
    successors = (ROOT / "src/aigineering/plugins/task_successors.py").read_text()
    core = "\n".join(
        path.read_text() for path in (ROOT / "src/aigineering/core").glob("*.py")
    )
    assert '"task.revise": EffectHandler(' in revision
    assert 'capability="task.revise"' in revision
    assert "projector=project_task_revision_effect" in revision
    assert "emitted_fact_types=frozenset({ASSIGNMENT_FACT})" in revision
    assert (
        "assignment_admission=AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR" in revision
    )
    assert "validate_task_revision_references" in revision
    assert "build_successor_contract(" in revision
    assert "freeze_successor_disclosure(" in revision
    assert "def freeze_successor_disclosure(" in successors
    assert "def project_task_revision_effect" not in core
    assert "build_successor_contract(" not in core
    assert "from aigineering.plugins.task_revision" not in core
    assert "from aigineering.plugins.task_successors" not in core
    assert "def remaining_output_slots(" in successors
