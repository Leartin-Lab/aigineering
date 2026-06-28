"""Tests for the CLI aig asset add/ls/show commands."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


class TestAssetAdd:
    """Tests for aig asset add."""

    def test_add_inline_content_succeeds(self):
        """aig asset add --name foo --content "bar" succeeds."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "foo", "--content", "bar"],
            )
            assert result.exit_code == 0, result.output
            assert "Asset injected: foo" in result.output

    def test_add_with_content_file_succeeds(self, tmp_path: Path):
        """aig asset add with --content-file reads from file."""
        content_file = tmp_path / "data.txt"
        content_file.write_text("file content")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "from_file",
                    "--content-file",
                    str(content_file),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Asset injected: from_file" in result.output

    def test_add_with_content_json_succeeds(self, tmp_path: Path):
        """aig asset add with --content-json reads and validates JSON."""
        json_file = tmp_path / "data.json"
        json_file.write_text('{"key": "value"}')

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "json_asset",
                    "--content-json",
                    str(json_file),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Asset injected: json_asset" in result.output

    def test_add_with_invalid_content_json_fails(self, tmp_path: Path):
        """aig asset add with --content-json pointing to invalid JSON fails."""
        json_file = tmp_path / "bad.json"
        json_file.write_text("not json")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "bad",
                    "--content-json",
                    str(json_file),
                ],
            )
            assert result.exit_code != 0
            assert "not valid JSON" in result.output

    def test_add_protected_name_rejected(self):
        """aig asset add --name _sys_secret is rejected (protected prefix)."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "_sys_secret", "--content", "x"],
            )
            assert result.exit_code != 0
            assert "protected" in result.output.lower()

    def test_add_content_file_and_json_mutual_exclusion(self, tmp_path: Path):
        """--content-file and --content-json are mutually exclusive."""
        txt = tmp_path / "data.txt"
        txt.write_text("text")
        json_file = tmp_path / "data.json"
        json_file.write_text('{"a": 1}')

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "conflict",
                    "--content-file",
                    str(txt),
                    "--content-json",
                    str(json_file),
                ],
            )
            assert result.exit_code != 0
            assert "content-file" in result.output.lower()

    def test_add_content_and_content_file_mutual_exclusion(self, tmp_path: Path):
        """--content is mutually exclusive with --content-file."""
        txt = tmp_path / "data.txt"
        txt.write_text("text")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "conflict",
                    "--content",
                    "inline",
                    "--content-file",
                    str(txt),
                ],
            )
            assert result.exit_code != 0

    def test_add_missing_content_fails(self):
        """asset add without any content source fails."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "orphan"],
            )
            assert result.exit_code != 0

    def test_add_json_output(self):
        """aig asset add --json returns valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "foo", "--content", "bar", "--json"],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["name"] == "foo"
            assert "id" in data
            assert data["trust_tier"] == "human"

    def test_add_with_origin_and_trust_tier(self):
        """aig asset add accepts --origin and --trust-tier options."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "trusted",
                    "--content",
                    "data",
                    "--origin",
                    "admin",
                    "--trust-tier",
                    "verified",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "trust_tier: verified" in result.output

    def test_add_with_no_promptable(self):
        """aig asset add --no-promptable marks asset as not promptable."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "secret",
                    "--content",
                    "secret_data",
                    "--no-promptable",
                ],
            )
            assert result.exit_code == 0, result.output
            # Verify via show --json
            result2 = runner.invoke(
                cli,
                ["asset", "show", "secret", "--json"],
            )
            assert result2.exit_code == 0
            data = json.loads(result2.output)
            assert data["promptable"] is False


class TestAssetList:
    """Tests for aig asset ls."""

    def test_list_empty(self):
        """aig asset ls on an empty store shows no assets."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["asset", "ls"])
            assert result.exit_code == 0
            assert "No assets found" in result.output

    def test_list_after_add(self):
        """aig asset ls shows injected assets."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "alpha", "--content", "a"])
            runner.invoke(cli, ["asset", "add", "--name", "beta", "--content", "b"])
            result = runner.invoke(cli, ["asset", "ls"])
            assert result.exit_code == 0
            assert "alpha" in result.output
            assert "beta" in result.output

    def test_list_json_output(self):
        """aig asset ls --json returns valid JSON array."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "x", "--content", "y"])
            result = runner.invoke(cli, ["asset", "ls", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["name"] == "x"


class TestAssetShow:
    """Tests for aig asset show."""

    def test_show_existing_asset(self):
        """aig asset show <name> displays metadata and content."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "foo", "--content", "bar"])
            result = runner.invoke(cli, ["asset", "show", "foo"])
            assert result.exit_code == 0
            assert "name:            foo" in result.output
            assert "--- content ---" in result.output
            assert "bar" in result.output

    def test_show_nonexistent_asset_fails(self):
        """aig asset show <missing> fails with an error."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["asset", "show", "nope"])
            assert result.exit_code != 0
            assert "No asset named" in result.output

    def test_show_json_output(self):
        """aig asset show <name> --json returns valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "foo", "--content", "bar"])
            result = runner.invoke(cli, ["asset", "show", "foo", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["name"] == "foo"
            assert data["content"] == "bar"
            assert data["content_type"] == "text"
            assert data["origin"] == "human"
            assert "id" in data
            assert "definition_hash" in data
            assert "content_hash" in data
            assert "signed_by" in data
            assert "promptable" in data

    def test_show_json_is_valid_json(self):
        """aig asset show --json output is parseable as valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "foo", "--content", "bar"])
            result = runner.invoke(cli, ["asset", "show", "foo", "--json"])
            assert result.exit_code == 0
            json.loads(result.output)  # does not raise


class TestAssetVersionWorkflow:
    """Tests for asset slice / replacement / versions / lineage workflows."""

    def test_slice_lines_range_extracts_subset(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "doc",
                    "--content",
                    "one\ntwo\nthree\nfour\n",
                ],
            )
            assert result.exit_code == 0, result.output

            result = runner.invoke(
                cli,
                [
                    "asset",
                    "slice",
                    "doc",
                    "--slice-name",
                    "doc.middle",
                    "--range",
                    "lines:2-3",
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["name"] == "doc.middle"

            show = runner.invoke(cli, ["asset", "show", "doc.middle", "--json"])
            assert show.exit_code == 0, show.output
            sliced = json.loads(show.output)
            assert sliced["content"] == "two\nthree\n"

            store = SQLiteStore(".aig/store.db")
            injected = store.get_by_event_type("asset_injected")
            assert any(
                e.relation_type == "asset_slice" and e.relation_target == "doc.middle"
                for e in injected
            )

    def test_slice_invalid_range_fails_closed(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "doc", "--content", "abc"])
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "slice",
                    "doc",
                    "--slice-name",
                    "bad",
                    "--range",
                    "bytes:1-2",
                ],
            )
            assert result.exit_code != 0
            assert "range_spec" in result.output or "range" in result.output

    def test_replacement_claim_verifies(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            a = runner.invoke(
                cli,
                ["asset", "add", "--name", "report", "--content", "v1", "--json"],
            )
            b = runner.invoke(
                cli,
                ["asset", "add", "--name", "report", "--content", "v2", "--json"],
            )
            assert a.exit_code == 0, a.output
            assert b.exit_code == 0, b.output
            source_id = json.loads(a.output)["id"]
            replacement_id = json.loads(b.output)["id"]

            claim = runner.invoke(
                cli,
                ["asset", "replace", source_id, replacement_id, "--json"],
            )
            assert claim.exit_code == 0, claim.output

            verify = runner.invoke(cli, ["verify", "replacements", "--json"])
            assert verify.exit_code == 0, verify.output
            result = json.loads(verify.output)
            assert result["pass_count"] == 1
            assert result["fail_count"] == 0

            store = SQLiteStore(".aig/store.db")
            events = store.get_by_event_type("replacement_claim_created")
            assert len(events) == 1
            assert events[0].relation_type == "replacement"
            assert events[0].relation_target == replacement_id
