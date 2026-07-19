"""Shared test fixtures for the aigineering test suite."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from aigineering.core.sqlite_store import SQLiteStore


@dataclass
class CandidateTestRuntime:
    """Explicit Candidate publisher for test setup; never bypasses commitment."""

    publisher: object
    genesis: object
    actor_key: object
    signer: object
    _sequence: int = 0

    def _publish(self, effect):
        self._sequence += 1
        decision = self.publisher.publish(
            (effect,), idempotency_key=f"test-setup-{self._sequence}"
        )
        if not decision.accepted:
            reasons = [
                str(record.payload.get("reason", ""))
                for record in decision.runtime_records
                if record.record_type.endswith("rejected")
            ]
            raise AssertionError(reasons or decision.runtime_records)
        return decision

    def accept_asset(self, asset, **_ignored):
        from aigineering.protocol.effect_builders import asset_proposal_effect

        decision = self._publish(asset_proposal_effect(asset))
        return decision.assets[0]

    def accept_contract(self, contract, **_ignored):
        from aigineering.core.ids import contract_identity_v3
        from aigineering.protocol.effect_builders import contract_declaration_effect

        if not contract.id.startswith("task:v3:"):
            contract = replace(contract, id=contract_identity_v3(contract))
        decision = self._publish(contract_declaration_effect(contract))
        assert decision.contract is not None
        return decision.contract

    def accept_replacement_claim(self, claim, **_ignored):
        from aigineering.protocol.effect_builders import replacement_claim_effect

        self._publish(replacement_claim_effect(claim))
        return claim


def candidate_runtime(
    store, trace=None, *, genesis=None, actor_key=None, signer=None
) -> CandidateTestRuntime:
    """Create one fully authorized Candidate publisher for test data setup."""
    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.signing import Ed25519Signer
    from aigineering.protocol.candidate import ActorKey, create_genesis_manifest

    trace = store if trace is None else trace
    if genesis is None:
        signer = Ed25519Signer()
        actor_key = ActorKey(
            "test:setup",
            "test-setup-1",
            signer.kind,
            signer.signer_id,
            (
                "actor.authorize",
                "asset.publish",
                "asset.publish.protected",
                "asset.relate",
                "contract.publish",
                "contract.publish.protected",
                "worker.register",
            ),
        )
        genesis = create_genesis_manifest(
            "test-candidate-runtime", (actor_key,), "policy:test-setup"
        )
        initialize_genesis(store, genesis)
    if actor_key is None or signer is None:
        raise ValueError("existing test domain requires its actor key and signer")
    return CandidateTestRuntime(
        CandidatePublisher(store, trace, genesis, actor_key, signer),
        genesis,
        actor_key,
        signer,
    )


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
