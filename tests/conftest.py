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


def hosted_worker(
    store, worker, *, genesis=None, authority_key=None, authority_signer=None
):
    """Authorize a test Worker without introducing a production trust shortcut."""
    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.signing import Ed25519Signer
    from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
    from aigineering.worker_hosting import authorize_worker_host

    if genesis is None:
        authority_signer = Ed25519Signer()
        authority_key = ActorKey(
            "test:authority",
            "test-authority-1",
            authority_signer.kind,
            authority_signer.signer_id,
            ("actor.authorize", "worker.register"),
        )
        genesis = create_genesis_manifest(
            "test-worker-host", (authority_key,), "policy:test-worker-host"
        )
        initialize_genesis(store, genesis)
    if authority_key is None or authority_signer is None:
        raise ValueError("existing test domain requires its authority key and signer")

    existing = store.get_worker_registration(worker.worker_id)
    if existing is not None:
        worker.registration = lambda: existing
    signer = Ed25519Signer()
    key = ActorKey(
        worker.worker_id,
        f"key:{worker.worker_id}",
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    authority = CandidatePublisher(
        store, store, genesis, authority_key, authority_signer
    )
    return authorize_worker_host(worker, genesis, key, signer, authority)


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
