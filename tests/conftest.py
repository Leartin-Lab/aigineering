"""Shared test fixtures for the aigineering test suite."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from aigineering.core.sqlite_store import SQLiteStore


def rmtree_retrying_permissions(path: str | Path, retries: int = 5) -> None:
    """Remove temp trees on Windows where SQLite/WAL handles close lazily."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError as e:
            last_error = e
            time.sleep(0.05 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)
    if last_error is not None and Path(path).exists():
        raise last_error


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
        rmtree_retrying_permissions(tmpdir)


def run_with_crash(
    crash_point: str, script: str, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run *script* as a subprocess with AIG_CRASH_POINT set.

    Returns the CompletedProcess.  The subprocess is expected to exit
    with code 1 (os._exit).
    """
    full_env = {
        **os.environ,
        "AIG_ENABLE_CRASH_INJECTION": "1",
        "AIG_CRASH_POINT": crash_point,
    }
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", script],
        env=full_env,
        capture_output=True,
        text=True,
    )
