"""Tests for aig capability descriptor commands."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


@pytest.fixture(autouse=True)
def initialize_candidate_domain_before_capability_publication(monkeypatch):
    original = CliRunner.invoke

    def invoke(runner, command, args=None, *positional, **kwargs):
        effective = list(args or ())
        if (
            effective[:2]
            in (
                ["capability", "add-tool"],
                ["capability", "add-memory"],
                ["capability", "add-persona"],
            )
            and not Path(".aig/identity/root.ed25519").exists()
        ):
            initialized = original(runner, command, ["domain", "init"])
            assert initialized.exit_code == 0, initialized.output
        return original(runner, command, args, *positional, **kwargs)

    monkeypatch.setattr(CliRunner, "invoke", invoke)


def test_capability_add_tool_list_show(tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}))

    runner = CliRunner()
    with runner.isolated_filesystem():
        added = runner.invoke(
            cli,
            [
                "capability",
                "add-tool",
                "--name",
                "lookup",
                "--description",
                "Lookup data",
                "--input-schema-json",
                str(schema),
                "--json",
            ],
        )
        assert added.exit_code == 0, added.output
        assert json.loads(added.output)["name"] == "_tool_capability_lookup"

        listed = runner.invoke(cli, ["capability", "list", "--json"])
        assert listed.exit_code == 0, listed.output
        rows = json.loads(listed.output)
        assert [row["name"] for row in rows] == ["_tool_capability_lookup"]

        shown = runner.invoke(
            cli,
            ["capability", "show", "_tool_capability_lookup", "--json"],
        )
        assert shown.exit_code == 0, shown.output
        content = json.loads(shown.output)["content"]
        assert content["kind"] == "tool"
        assert content["description"] == "Lookup data"
        assert content["sealed_config_ref"] == ""

        store = SQLiteStore(".aig/store.db")
        injected = store.get_by_event_type("candidate_committed")
        assert any(
            "_tool_capability_lookup" in e.accepted_asset_names for e in injected
        )


def test_capability_add_memory_and_persona(tmp_path: Path):
    persona = tmp_path / "persona.md"
    persona.write_text("Be exact.")

    runner = CliRunner()
    with runner.isolated_filesystem():
        memory = runner.invoke(
            cli,
            [
                "capability",
                "add-memory",
                "--name",
                "session",
                "--source-uri",
                "memory://session",
                "--json",
            ],
        )
        assert memory.exit_code == 0, memory.output

        persona_result = runner.invoke(
            cli,
            [
                "capability",
                "add-persona",
                "--name",
                "auditor",
                "--file",
                str(persona),
                "--json",
            ],
        )
        assert persona_result.exit_code == 0, persona_result.output

        listed = runner.invoke(cli, ["capability", "list", "--json"])
        assert listed.exit_code == 0, listed.output
        rows = json.loads(listed.output)
        assert [row["name"] for row in rows] == [
            "_memory_capability_session",
            "_persona_capability_auditor",
        ]


def test_capability_rejects_untrusted_descriptor(tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}))

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "capability",
                "add-tool",
                "--name",
                "unsafe",
                "--input-schema-json",
                str(schema),
                "--trust-tier",
                "untrusted",
            ],
        )
        assert result.exit_code != 0
        assert "failed trust gate" in result.output


def test_capability_rejects_non_object_schema(tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text("[]")

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "capability",
                "add-tool",
                "--name",
                "bad",
                "--input-schema-json",
                str(schema),
            ],
        )
        assert result.exit_code != 0
        assert "must contain a JSON object" in result.output
