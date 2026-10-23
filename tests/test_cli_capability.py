"""Tests for aig capability descriptor commands."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import aigineering.cli.capability as capability_cli
import aigineering.cli.infrastructure as cli_infrastructure
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


class _MemoryCapabilityProjection:
    def __init__(self):
        self.assets = []

    def get_all_assets(self):
        return list(self.assets)

    def get_assets_by_name(self, name):
        return [asset for asset in self.assets if asset.name == name]

    def get_asset(self, asset_id):
        return next((asset for asset in self.assets if asset.id == asset_id), None)


@pytest.fixture
def memory_capability_cli(monkeypatch):
    projection = _MemoryCapabilityProjection()
    monkeypatch.setattr(cli_infrastructure, "require_redis", lambda: None)
    monkeypatch.setattr(capability_cli, "_persistent_store", lambda: projection)
    monkeypatch.setattr(capability_cli, "_query_projection", lambda _store: projection)
    monkeypatch.setattr(
        capability_cli,
        "_store_descriptor",
        lambda descriptor, _kind: projection.assets.append(descriptor) or descriptor,
    )
    return projection


def _mark_domain_initialized() -> None:
    marker = Path(".aig/identity/root.ed25519")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


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
        added_data = json.loads(added.output)
        assert added_data["name"] == "_tool_capability_lookup"
        assert added_data["content"]["version"] == "0.1.0"

        listed = runner.invoke(cli, ["capability", "list", "--json"])
        assert listed.exit_code == 0, listed.output
        rows = json.loads(listed.output)
        assert [row["name"] for row in rows] == ["_tool_capability_lookup"]

        shown = runner.invoke(
            cli,
            ["capability", "show", "lookup", "--json"],
        )
        assert shown.exit_code == 0, shown.output
        content = json.loads(shown.output)["content"]
        assert content["kind"] == "tool"
        assert content["description"] == "Lookup data"
        assert content["sealed_config_ref"] == ""

        shown_by_id = runner.invoke(
            cli, ["capability", "show", "--id", added_data["id"], "--json"]
        )
        assert shown_by_id.exit_code == 0, shown_by_id.output
        assert json.loads(shown_by_id.output)["id"] == added_data["id"]

        store = SQLiteStore(".aig/store.db")
        injected = store.get_by_event_type("candidate_committed")
        assert any(
            "_tool_capability_lookup" in e.accepted_asset_names for e in injected
        )


def test_capability_add_tool_publishes_complete_descriptor(
    tmp_path: Path, memory_capability_cli
):
    input_schema = tmp_path / "input.json"
    input_schema.write_text(json.dumps({"type": "object", "required": ["q"]}))
    output_schema = tmp_path / "output.json"
    output_schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"success": {"const": True}},
                "required": ["success"],
            }
        )
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        _mark_domain_initialized()
        result = runner.invoke(
            cli,
            [
                "capability",
                "add-tool",
                "--name",
                "lookup",
                "--input-schema-json",
                str(input_schema),
                "--output-schema-json",
                str(output_schema),
                "--version",
                "2.3.4",
                "--max-output-bytes",
                "8192",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        content = json.loads(result.output)["content"]
        assert content["version"] == "2.3.4"
        assert content["input_schema"] == {
            "type": "object",
            "required": ["q"],
        }
        assert content["output_schema"] == {
            "type": "object",
            "properties": {"success": {"const": True}},
            "required": ["success"],
        }
        assert content["max_output_bytes"] == 8192
        assert len(memory_capability_cli.assets) == 1

        by_stored_name = runner.invoke(
            cli, ["capability", "show", "_tool_capability_lookup", "--json"]
        )
        assert by_stored_name.exit_code == 0, by_stored_name.output
        assert (
            json.loads(by_stored_name.output)["id"] == json.loads(result.output)["id"]
        )


@pytest.mark.parametrize(
    ("option", "value", "expected"),
    [
        ("--output-schema-json", "unsupported-schema.json", "unsupported JSON schema"),
        ("--max-output-bytes", "0", "Invalid value"),
    ],
)
def test_capability_add_tool_rejects_invalid_contract_without_publication(
    tmp_path: Path,
    memory_capability_cli,
    option: str,
    value: str,
    expected: str,
):
    input_schema = tmp_path / "input.json"
    input_schema.write_text(json.dumps({"type": "object"}))
    invalid_output_schema = tmp_path / "unsupported-schema.json"
    invalid_output_schema.write_text(json.dumps({"type": "object", "pattern": "x"}))

    runner = CliRunner()
    with runner.isolated_filesystem():
        _mark_domain_initialized()
        args = [
            "capability",
            "add-tool",
            "--name",
            "invalid",
            "--input-schema-json",
            str(input_schema),
            option,
            str(invalid_output_schema) if option == "--output-schema-json" else value,
        ]
        result = runner.invoke(cli, args)

        assert result.exit_code != 0
        assert expected in result.output
        assert (
            memory_capability_cli.get_assets_by_name("_tool_capability_invalid") == []
        )


def test_capability_show_requires_exact_selector_when_name_is_ambiguous(
    tmp_path: Path, memory_capability_cli
):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}))
    runner = CliRunner()
    with runner.isolated_filesystem():
        _mark_domain_initialized()
        results = [
            runner.invoke(
                cli,
                [
                    "capability",
                    "add-tool",
                    "--name",
                    "shared",
                    "--input-schema-json",
                    str(schema),
                    "--version",
                    version,
                    "--json",
                ],
            )
            for version in ("1.0.0", "2.0.0")
        ]
        assert all(result.exit_code == 0 for result in results)
        first, second = [json.loads(result.output) for result in results]

        ambiguous = runner.invoke(cli, ["capability", "show", "shared"])
        assert ambiguous.exit_code != 0
        assert "ambiguous" in ambiguous.output.lower()
        assert first["id"] in ambiguous.output
        assert second["id"] in ambiguous.output

        by_id = runner.invoke(
            cli, ["capability", "show", "--id", second["id"], "--json"]
        )
        assert by_id.exit_code == 0, by_id.output
        assert json.loads(by_id.output)["id"] == second["id"]

        by_storage_name = runner.invoke(
            cli,
            ["capability", "show", "_tool_capability_shared", "--id", second["id"]],
        )
        assert by_storage_name.exit_code != 0
        assert "exactly one" in by_storage_name.output


def test_capability_show_reports_cross_kind_logical_name_ambiguity(
    tmp_path: Path, memory_capability_cli
):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}))
    runner = CliRunner()
    with runner.isolated_filesystem():
        _mark_domain_initialized()
        tool = runner.invoke(
            cli,
            [
                "capability",
                "add-tool",
                "--name",
                "shared",
                "--input-schema-json",
                str(schema),
                "--json",
            ],
        )
        memory = runner.invoke(
            cli,
            [
                "capability",
                "add-memory",
                "--name",
                "shared",
                "--source-uri",
                "memory://shared",
                "--json",
            ],
        )
        assert tool.exit_code == 0, tool.output
        assert memory.exit_code == 0, memory.output
        tool_data, memory_data = json.loads(tool.output), json.loads(memory.output)

        ambiguous = runner.invoke(cli, ["capability", "show", "shared"])
        assert ambiguous.exit_code != 0
        assert tool_data["id"] in ambiguous.output
        assert memory_data["id"] in ambiguous.output


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
