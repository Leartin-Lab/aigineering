"""Skill loader — scans directories for skill.toml manifests and loads skills.

Each skill is described by a ``skill.toml`` manifest (TOML) in its own
directory.  The loader validates the manifest, creates a capability
descriptor Asset via :func:`create_skill_descriptor`, and loads the
skill body as a separate promptable content asset.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from aigineering.core.capability_descriptors import create_skill_descriptor
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.protocol.types import Asset, TrustTier

if TYPE_CHECKING:
    from aigineering.core.runtime_ingress import RuntimeIngress

_REQUIRED_MANIFEST_FIELDS = frozenset({"name", "version"})
_OPTIONAL_MANIFEST_FIELDS = frozenset(
    {"capabilities", "labels", "trust_tier", "description", "content_file"}
)
_VALID_FIELDS = _REQUIRED_MANIFEST_FIELDS | _OPTIONAL_MANIFEST_FIELDS


class StoreLike(Protocol):
    def add_asset(self, asset: Asset) -> None: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...


class SkillManifest:
    """Parsed and validated skill.toml manifest."""

    def __init__(self, data: dict, directory: Path) -> None:
        self.name: str = data.get("name", "")
        self.version: str = data.get("version", "")
        self.capabilities: list[str] = data.get("capabilities", [])
        self.labels: list[str] = data.get("labels", [])
        self.trust_tier: str = data.get("trust_tier", "configured")
        self.description: str = data.get("description", "")
        self.content_file: str = data.get("content_file", "skill.md")
        self.directory: Path = directory

    @property
    def content_path(self) -> Path:
        return self.directory / self.content_file


class SkillLoader:
    """Scans directories for ``skill.toml`` manifests and loads skills.

    Each skill directory must contain a ``skill.toml`` manifest with at
    least ``name`` and ``version`` fields.  The skill body (default:
    ``skill.md``) is loaded as a separate promptable content asset.
    """

    def __init__(self) -> None:
        self._manifests: list[SkillManifest] = []

    def scan(self, skill_dirs: list[str]) -> list[SkillManifest]:
        """Scan *skill_dirs* for ``skill.toml`` manifests.

        Returns the list of validated manifests (not yet loaded into a store).
        """
        manifests: list[SkillManifest] = []
        for skill_dir in skill_dirs:
            root = Path(skill_dir)
            if not root.is_dir():
                continue
            for manifest_path in root.rglob("skill.toml"):
                manifests.append(self._parse_manifest(manifest_path))
        self._manifests = manifests
        return manifests

    def load(self, store: StoreLike, *, ingress: RuntimeIngress) -> list[Asset]:
        """Load all scanned skills into *store*.

        Returns the list of descriptor Assets created.
        """
        descriptors: list[Asset] = []
        for manifest in self._manifests:
            descriptors.extend(self._load_one(store, manifest, ingress=ingress))
        return descriptors

    def _parse_manifest(self, manifest_path: Path) -> SkillManifest:
        """Parse and validate a single skill.toml manifest."""
        try:
            raw = manifest_path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"Failed to parse {manifest_path}: {e}") from e

        if not isinstance(data, dict):
            raise ValueError(f"{manifest_path}: skill.toml must contain a TOML table")

        missing = _REQUIRED_MANIFEST_FIELDS - set(data.keys())
        if missing:
            raise ValueError(
                f"{manifest_path}: missing required fields: {sorted(missing)}"
            )

        unknown = set(data.keys()) - _VALID_FIELDS
        if unknown:
            raise ValueError(f"{manifest_path}: unknown fields: {sorted(unknown)}")
        try:
            TrustTier.from_str(data.get("trust_tier", "configured"))
        except ValueError as e:
            raise ValueError(f"{manifest_path}: invalid trust_tier") from e
        for field_name in ("capabilities", "labels"):
            if field_name in data and not _is_string_list(data[field_name]):
                raise ValueError(
                    f"{manifest_path}: {field_name} must be a list of strings"
                )
        if "content_file" in data and not isinstance(data["content_file"], str):
            raise ValueError(f"{manifest_path}: content_file must be a string")
        content_file = data.get("content_file", "skill.md")
        if Path(content_file).is_absolute():
            raise ValueError(f"{manifest_path}: content_file must be relative")
        skill_root = manifest_path.parent.resolve()
        content_path = (manifest_path.parent / content_file).resolve()
        try:
            content_path.relative_to(skill_root)
        except ValueError as e:
            raise ValueError(
                f"{manifest_path}: content_file must stay within the skill directory"
            ) from e

        return SkillManifest(data, manifest_path.parent)

    def _load_one(
        self,
        store: StoreLike,
        manifest: SkillManifest,
        *,
        ingress: RuntimeIngress,
    ) -> list[Asset]:
        """Load a single skill into the store.  Returns descriptor Assets."""
        descriptors: list[Asset] = []

        # ── Read skill body content ──────────────────────────────────────
        content = ""
        if manifest.content_path.exists():
            content = manifest.content_path.read_text()

        # ── Create descriptor Asset (metadata only per ADR-005/007) ──────
        descriptor = create_skill_descriptor(
            name=manifest.name,
            content=content,
            trust_tier=manifest.trust_tier,
        )
        descriptors.append(
            ingress.accept_asset(
                descriptor, source="skill_loader", allow_protected=True
            )
        )

        # ── Create content Asset (promptable) ────────────────────────────
        content_asset_name = f"_skill_content_{manifest.name}"
        content_asset = Asset(
            id=hash_asset_content(content_asset_name, content),
            name=content_asset_name,
            content=content,
            content_type="text",
            definition_hash=hash_asset_definition(content_asset_name),
            content_hash=hash_asset_content(content_asset_name, content),
            origin="skill_loader",
            trust_tier=manifest.trust_tier,
            minted_by="skill_loader",
            source_uri=str(manifest.content_path.resolve()),
            promptable=True,
        )
        signed_content = ingress.accept_asset(
            content_asset, source="skill_loader", allow_protected=True
        )
        descriptors.append(signed_content)

        return descriptors


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def load_skills(
    store: StoreLike,
    skill_dirs: list[str],
    *,
    ingress: RuntimeIngress,
) -> list[Asset]:
    """Discover and load all skills from *skill_dirs* into *store*.

    Convenience entry point that scans and loads in one call.  Returns
    the list of descriptor Assets created.
    """
    loader = SkillLoader()
    loader.scan(skill_dirs)
    return loader.load(store, ingress=ingress)
