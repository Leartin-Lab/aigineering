"""Aigineering — Agent Runtime with structural hallucination containment."""

from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
import tomllib


def _source_version() -> str | None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.exists():
        return None
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    project = data.get("project", {})
    value = project.get("version")
    return value if isinstance(value, str) else None


__version__ = _source_version()
if __version__ is None:
    try:
        __version__ = version("aigineering")
    except PackageNotFoundError:
        __version__ = "0.0.0-dev"
