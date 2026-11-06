from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/aigineering"


def test_mechanical_output_source_has_one_core_owner_and_reader_floor():
    output = (SRC / "core/output_satisfaction.py").read_text()
    admission = (SRC / "core/contract_admission.py").read_text()
    migration = (SRC / "core/sqlite_migrations.py").read_text()
    assert "def output_asset_is_eligible(" in output
    assert 'policy.get("output_source") == "contract_tree"' in output
    assert "independent acceptance_policy cannot declare output_source" in admission
    assert "CURRENT_SCHEMA_VERSION = 22" in migration
    assert "def _migrate_to_v22(self)" in migration


def test_control_plane_defaults_to_strict_and_successors_reuse_core_selection():
    control_plane = (SRC / "core/control_plane.py").read_text()
    cli_common = (SRC / "cli/_common.py").read_text()
    successors = (SRC / "plugins/task_successors.py").read_text()
    assert '"output_source": "contract_tree"' in control_plane
    assert (
        'acceptance_policy={"mode": "mechanical", "output_source": "contract_tree"}'
        in cli_common
    )
    assert "output_asset_is_eligible(" in successors
    assert "_is_contract_or_descendant" not in successors
