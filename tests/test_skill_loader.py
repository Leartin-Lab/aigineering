"""Tests for the skill loader (core/skill_loader.py)."""
from pathlib import Path

from aigineering.core.skill_loader import SkillLoader, load_skills
from aigineering.core.store import MemoryStore


def _write_skill_toml(dir_path: Path, name: str, **kwargs) -> Path:
    """Helper to write a minimal skill.toml manifest."""
    manifest = {"name": name, "version": "0.1.0"}
    manifest.update(kwargs)
    lines = [f'{k} = "{v}"' if isinstance(v, str) else "" for k, v in manifest.items()]
    lines = [line for line in lines if line]
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


class TestSkillLoaderLoad:
    """Tests for SkillLoader.load()."""

    def test_load_creates_descriptor_and_content_assets(self, tmp_path: Path):
        """SkillLoader.load creates both descriptor and content assets."""
        skill_dir = tmp_path / "test_skill"
        skill_dir.mkdir()
        _write_skill_toml(
            skill_dir, "test_skill",
            version="0.1.0",
            trust_tier="configured",
        )
        (skill_dir / "skill.md").write_text("# Test Skill\n\nDo testing.")

        store = MemoryStore()
        loader = SkillLoader()
        loader.scan([str(tmp_path)])
        descriptors = loader.load(store)

        assert len(descriptors) == 2

        cap_asset = store.get_assets_by_name("_skill_capability_test_skill")
        assert len(cap_asset) == 1
        assert cap_asset[0].trust_tier == "configured"

        content_asset = store.get_assets_by_name("_skill_content_test_skill")
        assert len(content_asset) == 1
        assert "Test Skill" in content_asset[0].content
        assert content_asset[0].promptable is True

    def test_load_skill_without_content_file(self, tmp_path: Path):
        """SkillLoader.load handles missing skill.md gracefully."""
        skill_dir = tmp_path / "empty_skill"
        skill_dir.mkdir()
        _write_skill_toml(skill_dir, "empty_skill", version="0.1.0")
        # No skill.md — should create descriptor with empty content hash

        store = MemoryStore()
        loader = SkillLoader()
        loader.scan([str(tmp_path)])
        descriptors = loader.load(store)
        assert len(descriptors) == 2

    def test_load_skills_entry_point(self, tmp_path: Path):
        """load_skills convenience function works."""
        skill_dir = tmp_path / "convenience"
        skill_dir.mkdir()
        _write_skill_toml(skill_dir, "convenience", version="0.1.0")
        (skill_dir / "skill.md").write_text("Convenience test.")

        store = MemoryStore()
        descriptors = load_skills(store, [str(tmp_path)])
        assert len(descriptors) == 2
