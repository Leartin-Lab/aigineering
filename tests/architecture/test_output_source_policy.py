from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/aigineering"


def test_output_conditions_have_one_matcher_without_task_origin_or_tree_authority():
    output = (SRC / "core/output_satisfaction.py").read_text()
    admission = (SRC / "core/contract_admission.py").read_text()
    migration = (SRC / "core/sqlite_migrations.py").read_text()
    assert "def output_asset_meets_conditions(" in output
    assert "def unmet_output_names(" in output
    assert "def is_output_role_asset(" in output
    assert "output_requirements" in output
    assert "producer_actor_ids" in output
    assert "contract.origin" not in output
    assert "return _is_contract_or_descendant(" not in output
    assert 'policy.get("output_source")' not in output
    assert "validate_output_requirements(contract)" in admission
    assert "CURRENT_SCHEMA_VERSION = 24" in migration
    assert "def _migrate_to_v23(self)" in migration


def test_control_plane_defaults_to_mechanical_and_successors_filter_shared_policy():
    control_plane = (SRC / "core/control_plane.py").read_text()
    cli_common = (SRC / "cli/_common.py").read_text()
    assert 'else {"mode": "mechanical"}' in control_plane
    assert 'acceptance_policy={"mode": "mechanical"}' in cli_common
    assert not (SRC / "plugins/task_successors.py").exists()
