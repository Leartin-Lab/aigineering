"""Tests for the public CLI demo paths."""

from click.testing import CliRunner

from aigineering.cli.main import cli


def test_audit_accepts_asset_name_via_asset_option():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["audit", "--asset-name", "final_report"],
    )

    assert result.exit_code == 0
    assert "final_report" in result.output
    assert "projection from candidate" in result.output
    assert "disclosure:" in result.output
    assert "activation:" in result.output
