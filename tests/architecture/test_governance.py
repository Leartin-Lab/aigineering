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
