"""Tests for the CLI aig behavior add/list/show commands."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli


@pytest.fixture(autouse=True)
def initialize_candidate_domain_before_behavior_publication(monkeypatch):
    original = CliRunner.invoke

    def invoke(runner, command, args=None, *positional, **kwargs):
        effective = list(args or ())
        if (
            effective[:2] == ["behavior", "add"]
            and not Path(".aig/identity/root.ed25519").exists()
        ):
            initialized = original(runner, command, ["domain", "init"])
            assert initialized.exit_code == 0, initialized.output
        return original(runner, command, args, *positional, **kwargs)

    monkeypatch.setattr(CliRunner, "invoke", invoke)


class TestBehaviorAdd:
    """Tests for aig behavior add."""

    def test_add_from_file_succeeds(self, tmp_path: Path):
        """aig behavior add --name foo --file path/to/foo.md succeeds."""
        md_file = tmp_path / "instructions.md"
        md_file.write_text("# Be helpful\n\nAlways be polite.")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "behavior",
                    "add",
                    "--name",
                    "helpful",
                    "--file",
                    str(md_file),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Behaviour asset injected: behavior:helpful" in result.output

    def test_add_json_output(self, tmp_path: Path):
        """aig behavior add --json returns valid JSON."""
        md_file = tmp_path / "instructions.md"
        md_file.write_text("Be concise.")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "behavior",
                    "add",
                    "--name",
                    "concise",
                    "--file",
                    str(md_file),
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["name"] == "behavior:concise"
            assert "id" in data
            assert data["trust_tier"] == "human"

    def test_add_with_custom_trust_tier(self, tmp_path: Path):
        """aig behavior add --trust-tier verified stores with that tier."""
        md_file = tmp_path / "instructions.md"
        md_file.write_text("Verify everything.")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "behavior",
                    "add",
                    "--name",
                    "verified_behavior",
                    "--file",
                    str(md_file),
                    "--trust-tier",
                    "verified",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "trust_tier: verified" in result.output

    def test_add_missing_file_fails(self):
        """aig behavior add --file nonexistent fails."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "behavior",
                    "add",
                    "--name",
                    "bad",
                    "--file",
                    "/nonexistent/path.md",
                ],
            )
            assert result.exit_code != 0


class TestBehaviorList:
    """Tests for aig behavior list."""

    def test_list_empty(self):
        """aig behavior list on empty store shows no behaviours."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["behavior", "list"])
            assert result.exit_code == 0
            assert "No behaviour assets found" in result.output

    def test_list_after_add(self, tmp_path: Path):
        """aig behavior list shows injected behaviours."""
        md_file = tmp_path / "a.md"
        md_file.write_text("behaviour A")
        md_file2 = tmp_path / "b.md"
        md_file2.write_text("behaviour B")

        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["behavior", "add", "--name", "alpha", "--file", str(md_file)],
            )
            runner.invoke(
                cli,
                ["behavior", "add", "--name", "beta", "--file", str(md_file2)],
            )
            result = runner.invoke(cli, ["behavior", "list"])
            assert result.exit_code == 0
            assert "alpha" in result.output
            assert "beta" in result.output

    def test_list_json_output(self, tmp_path: Path):
        """aig behavior list --json returns valid JSON array."""
        md_file = tmp_path / "x.md"
        md_file.write_text("behaviour X")

        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["behavior", "add", "--name", "x", "--file", str(md_file)],
            )
            result = runner.invoke(cli, ["behavior", "list", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["name"] == "behavior:x"


class TestBehaviorShow:
    """Tests for aig behavior show."""

    def test_show_existing_behavior(self, tmp_path: Path):
        """aig behavior show <name> displays metadata and content."""
        md_file = tmp_path / "instructions.md"
        md_file.write_text("# Be helpful\n\nAlways be polite.")

        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["behavior", "add", "--name", "helpful", "--file", str(md_file)],
            )
            result = runner.invoke(cli, ["behavior", "show", "helpful"])
            assert result.exit_code == 0
            assert "name:            behavior:helpful" in result.output
            assert "--- content ---" in result.output
            assert "Be helpful" in result.output

    def test_show_with_behavior_prefix(self, tmp_path: Path):
        """aig behavior show accepts the behavior: prefix."""
        md_file = tmp_path / "instructions.md"
        md_file.write_text("Custom instructions.")

        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["behavior", "add", "--name", "custom", "--file", str(md_file)],
            )
            result = runner.invoke(cli, ["behavior", "show", "behavior:custom"])
            assert result.exit_code == 0
            assert "Custom instructions" in result.output

    def test_show_nonexistent_fails(self):
        """aig behavior show <missing> fails with an error."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["behavior", "show", "nope"])
            assert result.exit_code != 0
            assert "No behaviour asset named" in result.output

    def test_show_json_output(self, tmp_path: Path):
        """aig behavior show <name> --json returns valid JSON."""
        md_file = tmp_path / "instructions.md"
        md_file.write_text("Custom instructions.")

        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["behavior", "add", "--name", "json_test", "--file", str(md_file)],
            )
            result = runner.invoke(cli, ["behavior", "show", "json_test", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["name"] == "behavior:json_test"
            assert data["content"] == "Custom instructions."
            assert data["content_type"] == "text"
            assert data["origin"] == "human"
            assert "id" in data
            assert "definition_hash" in data
            assert "content_hash" in data

    def test_list_excludes_non_behavior_assets(self, tmp_path: Path):
        """aig behavior list only shows behavior:* assets, not other assets."""
        md_file = tmp_path / "instructions.md"
        md_file.write_text("behaviour content")

        runner = CliRunner()
        with runner.isolated_filesystem():
            # Add a behaviour asset
            runner.invoke(
                cli,
                ["behavior", "add", "--name", "my_behavior", "--file", str(md_file)],
            )
            # Add a regular asset
            runner.invoke(
                cli,
                ["asset", "add", "--name", "regular_asset", "--content", "data"],
            )
            result = runner.invoke(cli, ["behavior", "list"])
            assert result.exit_code == 0
            assert "my_behavior" in result.output
            assert "regular_asset" not in result.output
