"""Executable constraints for the 0.5 design/change/evidence workflow."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_design_truth_and_active_change_are_present():
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    change = (ROOT / "changes/001-candidate-genesis.md").read_text(encoding="utf-8")

    assert "Implemented runtime path" in design
    assert "Known transition boundaries".lower() in design.lower()
    for section in (
        "## Problem",
        "## Resulting design",
        "## Compatibility sequence",
        "## Required architecture tests",
        "## Deletion ledger",
        "## Exit criteria",
    ):
        assert section in change


def test_legacy_runtime_files_stay_out_of_release_artifacts():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for path in (
        "src/aigineering/core/engine.py",
        "src/aigineering/core/startup_check.py",
        "src/aigineering/core/state_serializer.py",
    ):
        assert path in project


def test_contract_cli_uses_candidate_commitment_not_legacy_ingress():
    source = (ROOT / "src/aigineering/cli/contract.py").read_text(encoding="utf-8")

    assert "commit_local_effect" in source
    assert "contract_declaration_effect" in source
    assert "RuntimeIngress" not in source
    assert "inject_contract" not in source


def test_asset_add_uses_candidate_commitment_not_legacy_ingress():
    source = (ROOT / "src/aigineering/cli/asset.py").read_text(encoding="utf-8")
    add_body = source.split('@asset_group.command("ls")', 1)[0]

    assert "commit_local_effect" in add_body
    assert "asset_proposal_effect" in add_body
    assert "inject_asset" not in add_body
    assert "RuntimeIngress(" not in add_body


def test_task_create_uses_candidate_commitment_not_legacy_ingress():
    source = (ROOT / "src/aigineering/cli/task.py").read_text(encoding="utf-8")
    create_body = source.split('@task_group.command("status")', 1)[0]

    assert "commit_local_effect" in create_body
    assert "contract_declaration_effect" in create_body
    assert "inject_contract" not in create_body
    assert "RuntimeIngress(" not in create_body


def test_behavior_add_uses_asset_candidate_path():
    source = (ROOT / "src/aigineering/cli/behavior.py").read_text(encoding="utf-8")
    add_body = source.split('@behavior_group.command("list")', 1)[0]

    assert "commit_local_effect" in add_body
    assert "asset_proposal_effect" in add_body
    assert "inject_asset" not in add_body
    assert "RuntimeIngress(" not in add_body


def test_commitment_coordinator_does_not_own_effect_semantics():
    path = ROOT / "src/aigineering/core/commitment.py"
    source = path.read_text(encoding="utf-8")

    assert "asset.propose" not in source
    assert "contract.declare" not in source
    assert "scan_runtime_records" not in source
    assert len(source.splitlines()) < 300
