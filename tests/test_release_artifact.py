"""Release artifact boundaries for the stateless v0.5 runtime."""

from __future__ import annotations

from pathlib import Path
import tomllib


def test_all_release_artifacts_exclude_legacy_stateful_engine_modules():
    config = tomllib.loads(Path("pyproject.toml").read_text())
    required = {"src/aigineering/core/runtime_ingress.py"}

    for target in ("wheel", "sdist"):
        excluded = set(config["tool"]["hatch"]["build"]["targets"][target]["exclude"])
        assert required <= excluded
    assert not Path("src/aigineering/core/startup_check.py").exists()
    for removed in (
        "engine.py",
        "context_overflow.py",
        "method_registry.py",
        "method_runtime.py",
        "continuation_manager.py",
        "state_serializer.py",
    ):
        assert not Path("src/aigineering/core", removed).exists()
    assert not list(Path("src/aigineering/core/method_handlers").glob("*.py"))

    sdist_excluded = set(
        config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    )
    assert "tests/**" in sdist_excluded
