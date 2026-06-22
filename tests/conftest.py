"""Shared test fixtures for the aigineering test suite."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from aigineering.core.sqlite_store import SQLiteStore


@pytest.fixture
def temp_sqlite_store():
    """Create a temporary SQLiteStore with a unique database file in WAL mode."""
    tmpdir = tempfile.mkdtemp(prefix="aig_test_")
    db_path = Path(tmpdir) / "aig.db"
    store = SQLiteStore(str(db_path))
    try:
        yield store
    finally:
        store.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_with_crash(
    crash_point: str, script: str, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run *script* as a subprocess with AIG_CRASH_POINT set.

    Returns the CompletedProcess.  The subprocess is expected to exit
    with code 1 (os._exit).
    """
    full_env = {**os.environ, "AIG_ENABLE_CRASH_INJECTION": "1", "AIG_CRASH_POINT": crash_point}
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", script],
        env=full_env,
        capture_output=True,
        text=True,
    )
