"""Release artifact boundaries for the stateless v0.5 runtime."""

from __future__ import annotations

from pathlib import Path
import tomllib


def test_all_release_artifacts_exclude_legacy_stateful_engine_modules():
    config = tomllib.loads(Path("pyproject.toml").read_text())
    required = {
        "src/aigineering/core/engine.py",
        "src/aigineering/core/state_serializer.py",
    }

    for target in ("wheel", "sdist"):
        excluded = set(config["tool"]["hatch"]["build"]["targets"][target]["exclude"])
        assert required <= excluded
    assert not Path("src/aigineering/core/startup_check.py").exists()

    sdist_excluded = set(
        config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    )
    assert "tests/**" in sdist_excluded
