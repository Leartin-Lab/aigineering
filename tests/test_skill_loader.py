"""Tests for the skill loader (core/skill_loader.py)."""

from pathlib import Path
import json

from aigineering.core.skill_loader import SkillLoader, build_skill_assets


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML test value: {value!r}")


def _write_skill_toml(dir_path: Path, name: str, **kwargs) -> Path:
    """Helper to write a minimal skill.toml manifest."""
    manifest = {"name": name, "version": "0.1.0"}
    manifest.update(kwargs)
    lines = [f"{k} = {_toml_value(v)}" for k, v in manifest.items()]
    toml_path = dir_path / "skill.toml"
    toml_path.write_text("\n".join(lines) + "\n")
    return toml_path


class TestSkillLoaderScan:
    """Tests for SkillLoader.scan()."""

    def test_scan_single_manifest(self, tmp_path: Path):
        """SkillLoader.scan finds a single skill.toml."""
        skill_dir = tmp_path / "my_skill"
        skill_dir.mkdir()
        _write_skill_toml(skill_dir, "my_skill", version="0.1.0")

        loader = SkillLoader()
        manifests = loader.scan([str(tmp_path)])
        assert len(manifests) == 1
        assert manifests[0].name == "my_skill"
        assert manifests[0].version == "0.1.0"

    def test_scan_multiple_manifests(self, tmp_path: Path):
        """SkillLoader.scan finds multiple skills in nested dirs."""
        for i in range(3):
            skill_dir = tmp_path / f"skill_{i}"
            skill_dir.mkdir()
            _write_skill_toml(skill_dir, f"skill_{i}", version="1.0.0")

        loader = SkillLoader()
        manifests = loader.scan([str(tmp_path)])
        assert len(manifests) == 3
        names = {m.name for m in manifests}
        assert names == {"skill_0", "skill_1", "skill_2"}

    def test_scan_skips_non_directory(self, tmp_path: Path):
        """SkillLoader.scan ignores non-directory paths gracefully."""
        loader = SkillLoader()
        manifests = loader.scan([str(tmp_path / "nonexistent")])
        assert manifests == []

    def test_scan_missing_required_field_raises(self, tmp_path: Path):
        """SkillLoader.scan raises ValueError on missing required fields."""
        skill_dir = tmp_path / "bad_skill"
        skill_dir.mkdir()
        toml_path = skill_dir / "skill.toml"
        toml_path.write_text('version = "0.1.0"\n')  # missing name

        loader = SkillLoader()
        try:
            loader.scan([str(tmp_path)])
            assert False, "should have raised"
        except ValueError as e:
            assert "missing required fields" in str(e)

    def test_scan_invalid_trust_tier_raises(self, tmp_path: Path):
        skill_dir = tmp_path / "bad_tier"
        skill_dir.mkdir()
        (skill_dir / "skill.toml").write_text(
            'name = "bad_tier"\nversion = "0.1.0"\ntrust_tier = "banana"\n'
        )

        loader = SkillLoader()
        try:
            loader.scan([str(tmp_path)])
            assert False, "should have raised"
        except ValueError as e:
            assert "invalid trust_tier" in str(e)

    def test_scan_invalid_capabilities_type_raises(self, tmp_path: Path):
        skill_dir = tmp_path / "bad_caps"
        skill_dir.mkdir()
        (skill_dir / "skill.toml").write_text(
            'name = "bad_caps"\nversion = "0.1.0"\ncapabilities = "tool"\n'
        )

        loader = SkillLoader()
        try:
            loader.scan([str(tmp_path)])
            assert False, "should have raised"
        except ValueError as e:
            assert "capabilities must be a list of strings" in str(e)

    def test_scan_rejects_content_file_path_escape(self, tmp_path: Path):
        skill_dir = tmp_path / "escaping"
        skill_dir.mkdir()
        (skill_dir / "skill.toml").write_text(
            'name = "escaping"\nversion = "0.1.0"\ncontent_file = "../outside.md"\n'
        )

        loader = SkillLoader()
        try:
            loader.scan([str(tmp_path)])
            assert False, "should have raised"
        except ValueError as e:
            assert "content_file must stay within the skill directory" in str(e)

    def test_scan_rejects_absolute_content_file(self, tmp_path: Path):
        skill_dir = tmp_path / "absolute"
        skill_dir.mkdir()
        outside = tmp_path / "outside.md"
        _write_skill_toml(skill_dir, "absolute", content_file=str(outside))

        loader = SkillLoader()
        try:
            loader.scan([str(tmp_path)])
            assert False, "should have raised"
        except ValueError as e:
            assert "content_file must be relative" in str(e)


class TestSkillLoaderBuild:
    """Tests for pure SkillLoader Asset construction."""

    def test_load_creates_descriptor_and_content_assets(self, tmp_path: Path):
        """SkillLoader builds both descriptor and content assets."""
        skill_dir = tmp_path / "test_skill"
        skill_dir.mkdir()
        _write_skill_toml(
            skill_dir,
            "test_skill",
            version="0.1.0",
            trust_tier="configured",
        )
        (skill_dir / "skill.md").write_text("# Test Skill\n\nDo testing.")

        loader = SkillLoader()
        loader.scan([str(tmp_path)])
        descriptors = loader.build_assets()

        assert len(descriptors) == 2

        cap_asset = next(
            asset
            for asset in descriptors
            if asset.name == "_skill_capability_test_skill"
        )
        assert cap_asset.trust_tier == "configured"

        content_asset = next(
            asset for asset in descriptors if asset.name == "_skill_content_test_skill"
        )
        assert "Test Skill" in content_asset.content
        assert content_asset.promptable is True

    def test_load_skill_without_content_file(self, tmp_path: Path):
        """SkillLoader handles missing skill.md gracefully."""
        skill_dir = tmp_path / "empty_skill"
        skill_dir.mkdir()
        _write_skill_toml(skill_dir, "empty_skill", version="0.1.0")
        # No skill.md — should create descriptor with empty content hash

        loader = SkillLoader()
        loader.scan([str(tmp_path)])
        descriptors = loader.build_assets()
        assert len(descriptors) == 2

    def test_load_default_skill_is_configured_and_preserves_lists(self, tmp_path: Path):
        skill_dir = tmp_path / "listed_skill"
        skill_dir.mkdir()
        _write_skill_toml(
            skill_dir,
            "listed_skill",
            version="0.1.0",
            capabilities=["tool", "memory"],
            labels=["local", "safe"],
        )

        loader = SkillLoader()
        manifests = loader.scan([str(tmp_path)])

        assert manifests[0].trust_tier == "configured"
        assert manifests[0].capabilities == ["tool", "memory"]
        assert manifests[0].labels == ["local", "safe"]

    def test_build_skill_assets_entry_point(self, tmp_path: Path):
        """build_skill_assets convenience function works."""
        skill_dir = tmp_path / "convenience"
        skill_dir.mkdir()
        _write_skill_toml(skill_dir, "convenience", version="0.1.0")
        (skill_dir / "skill.md").write_text("Convenience test.")

        descriptors = build_skill_assets([str(tmp_path)])
        assert len(descriptors) == 2

    def test_nested_skill_loaded_once(self, tmp_path: Path):
        parent = tmp_path / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        _write_skill_toml(parent, "parent", version="0.1.0")
        _write_skill_toml(child, "child", version="0.1.0")
        (parent / "skill.md").write_text("Parent skill.")
        (child / "skill.md").write_text("Child skill.")

        descriptors = build_skill_assets([str(tmp_path)])

        assert len(descriptors) == 4
        names = {asset.name for asset in descriptors}
        assert names == {
            "_skill_capability_parent",
            "_skill_content_parent",
            "_skill_capability_child",
            "_skill_content_child",
        }
