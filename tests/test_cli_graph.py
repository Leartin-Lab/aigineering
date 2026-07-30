from __future__ import annotations

import json

from click.testing import CliRunner

from aigineering.cli.main import cli


def test_graph_cli_exposes_migrated_asset_view() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(cli, ["domain", "init", "--json"]).exit_code == 0
        added = runner.invoke(
            cli,
            ["asset", "add", "--name", "source", "--content", "evidence", "--json"],
        )
        assert added.exit_code == 0, added.output

        contents = runner.invoke(cli, ["graph", "contents", "--json"])
        definitions = runner.invoke(cli, ["graph", "definitions", "--json"])
        assertions = runner.invoke(cli, ["graph", "assertions", "--json"])

    assert contents.exit_code == definitions.exit_code == assertions.exit_code == 0
    content_values = json.loads(contents.output)
    definition_values = json.loads(definitions.output)
    assertion_values = json.loads(assertions.output)
    assert len(content_values) == len(definition_values) == len(assertion_values) == 1
    assert definition_values[0]["legacy_asset_id"]
    assert assertion_values[0]["content_id"] == content_values[0]["id"]
    assert assertion_values[0]["definition_id"] == definition_values[0]["id"]
