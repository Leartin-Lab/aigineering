"""Tests for aig mcp descriptor commands."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


def test_mcp_add_list_show_json():
    runner = CliRunner()
    with runner.isolated_filesystem():
        added = runner.invoke(
            cli,
            [
                "mcp",
                "add",
                "--name",
                "search",
                "--source-uri",
                "mcp://search",
                "--tool-name",
                "search.query",
                "--json",
            ],
        )
        assert added.exit_code == 0, added.output
        added_data = json.loads(added.output)
        assert added_data["name"] == "_mcp_search"
        assert added_data["trust_tier"] == "configured"

        listed = runner.invoke(cli, ["mcp", "list", "--json"])
        assert listed.exit_code == 0, listed.output
        rows = json.loads(listed.output)
        assert [row["name"] for row in rows] == ["_mcp_search"]

        shown = runner.invoke(cli, ["mcp", "show", "search", "--json"])
        assert shown.exit_code == 0, shown.output
        shown_data = json.loads(shown.output)
        assert shown_data["content"]["kind"] == "mcp"
        assert shown_data["content"]["tool_name"] == "search.query"
        assert shown_data["content"]["sealed_config_ref"] == ""

        store = SQLiteStore(".aig/store.db")
        injected = store.get_by_event_type("asset_injected")
        assert any(
            e.relation_type == "mcp_capability"
            and e.relation_target == "_mcp_search"
            for e in injected
        )


def test_mcp_add_loads_schema_files(tmp_path: Path):
    input_schema = tmp_path / "input.json"
    output_schema = tmp_path / "output.json"
    input_schema.write_text(
        json.dumps({"type": "object", "properties": {"q": {"type": "string"}}})
    )
    output_schema.write_text(json.dumps({"type": "object"}))

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "mcp",
                "add",
                "--name",
                "search",
                "--source-uri",
                "mcp://search",
                "--input-schema-json",
                str(input_schema),
                "--output-schema-json",
                str(output_schema),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output

        shown = runner.invoke(cli, ["mcp", "show", "_mcp_search", "--json"])
        assert shown.exit_code == 0, shown.output
        content = json.loads(shown.output)["content"]
        assert content["input_schema"]["properties"]["q"]["type"] == "string"
        assert content["output_schema"]["type"] == "object"


def test_mcp_add_rejects_untrusted_descriptor():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "mcp",
                "add",
                "--name",
                "unsafe",
                "--source-uri",
                "mcp://unsafe",
                "--trust-tier",
                "untrusted",
            ],
        )
        assert result.exit_code != 0
        assert "failed trust gate" in result.output


def test_mcp_add_rejects_non_object_schema(tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text("[]")

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "mcp",
                "add",
                "--name",
                "bad",
                "--source-uri",
                "mcp://bad",
                "--input-schema-json",
                str(schema),
            ],
        )
        assert result.exit_code != 0
        assert "must contain a JSON object" in result.output
