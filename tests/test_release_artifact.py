"""Release artifact boundaries for the stateless v0.5 runtime."""

from __future__ import annotations

from pathlib import Path
import tomllib


def test_all_release_artifacts_exclude_legacy_stateful_engine_modules():
    config = tomllib.loads(Path("pyproject.toml").read_text())
    assert "exclude" not in config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert not Path("src/aigineering/core/startup_check.py").exists()
    for removed in (
        "engine.py",
        "context_overflow.py",
        "method_registry.py",
        "method_runtime.py",
        "continuation_manager.py",
        "state_serializer.py",
        "runtime_ingress.py",
    ):
        assert not Path("src/aigineering/core", removed).exists()
    assert not list(Path("src/aigineering/core/method_handlers").glob("*.py"))

    sdist_excluded = set(
        config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    )
    assert "tests/**" in sdist_excluded
    assert Path("conformance/README.md").is_file()
    assert Path("conformance/v0.5.0/protocol-vectors.json").is_file()
