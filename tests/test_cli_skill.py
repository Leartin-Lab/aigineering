"""Tests for aig skill load/list."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


def _write_skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "skill.toml").write_text(
        f'name = "{name}"\nversion = "0.1.0"\ntrust_tier = "configured"\n'
    )
    (skill_dir / "skill.md").write_text(f"# {name}\n\nDo useful work.")
    return skill_dir


def test_skill_load_and_list_json(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "reviewer")
    runner = CliRunner()
    with runner.isolated_filesystem():
        loaded = runner.invoke(cli, ["skill", "load", str(skill_dir), "--json"])
        assert loaded.exit_code == 0, loaded.output
        data = json.loads(loaded.output)
        assert data["loaded_manifests"] == ["reviewer"]
        assert "_skill_capability_reviewer" in data["asset_names"]
        assert "_skill_content_reviewer" in data["asset_names"]

        listed = runner.invoke(cli, ["skill", "list", "--json"])
        assert listed.exit_code == 0, listed.output
        rows = json.loads(listed.output)
        assert len(rows) == 1
        assert rows[0]["skill"] == "reviewer"
        assert rows[0]["trust_tier"] == "configured"

        store = SQLiteStore(".aig/store.db")
        injected = store.get_by_event_type("asset_injected")
        injected_names = {e.relation_target for e in injected}
        assert "_skill_capability_reviewer" in injected_names
        assert "_skill_content_reviewer" in injected_names


def test_skill_load_invalid_manifest_fails(tmp_path: Path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "skill.toml").write_text('version = "0.1.0"\n')

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["skill", "load", str(bad)])
        assert result.exit_code != 0
        assert "missing required fields" in result.output
