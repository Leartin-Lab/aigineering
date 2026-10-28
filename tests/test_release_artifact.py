"""Release artifact boundaries for the stateless v0.5 runtime."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile


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
    assert Path("conformance/v0.5.2/asset-graph-vectors.json").is_file()


def test_distributions_include_public_files_and_exclude_private_work(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", "README.zh-CN.md", "LICENSE"):
        shutil.copyfile(name, project / name)
    shutil.copytree(
        "src", project / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    public_files = (
        "docs/README.md",
        "examples/example.txt",
        "conformance/example.json",
        "reports/example.md",
    )
    private_roots = (".task-evidence", ".worktrees", "." + "internal-docs", ".aig")
    private_files = tuple(f"{name}/private-canary.txt" for name in private_roots)
    private_files += ("AGENTS.md", ".env", "unpublished-notes.txt")
    for name in (*public_files, *private_files):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic distribution canary\n", encoding="utf-8")
    output = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(output),
            str(project),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        source_members = {name.partition("/")[2] for name in archive.getnames()}
    with zipfile.ZipFile(next(output.glob("*.whl"))) as archive:
        wheel_members = set(archive.namelist())
    assert set(public_files).issubset(source_members)
    assert {"README.md", "README.zh-CN.md", "src/aigineering/__init__.py"}.issubset(
        source_members
    )
    assert "aigineering/__init__.py" in wheel_members
    assert not set(private_files) & source_members
    assert not set(private_files) & wheel_members
    assert all(not name.startswith(private_roots) for name in wheel_members)
