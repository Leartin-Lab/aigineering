"""Same-domain active-active protocol proofs for the v0.5.0 runtime."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from conftest import candidate_runtime

from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.types import Contract


_CLAIM_SCRIPT = """
import sys
import time
from aigineering.runtime import claim_next_package
from aigineering.core.sqlite_store import SQLiteStore

db_path, contract_id, replica, start_at = sys.argv[1:]
time.sleep(max(0.0, float(start_at) - time.time()))
store = SQLiteStore(db_path)
claimed = claim_next_package(
    store,
    worker_id=f"replica-{replica}",
    contract_id=contract_id,
    lease_seconds=30,
)
print(claimed.package.claim_id if claimed is not None else "")
store.close()
"""


@pytest.mark.parametrize("replica_count", [2, 10])
def test_active_active_replicas_grant_one_invocation(tmp_path, replica_count):
    db_path = str(tmp_path / "active-active.db")
    store = SQLiteStore(db_path)
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(
        Contract(
            id=f"task:active-active:{replica_count}",
            outputs=("result",),
            budget=1,
            worker_capabilities=("active-active-test",),
        )
    )
    contract_id = contract.id
    for replica in range(replica_count):
        worker_id = f"replica-{replica}"
        signer = Ed25519Signer()
        key = ActorKey(
            worker_id,
            f"replica-key-{replica}",
            signer.kind,
            signer.signer_id,
            ("worker.submit",),
        )
        runtime.authorize_actor(key)
        runtime.register_worker(
            WorkerRegistration(
                worker_id,
                capabilities=("active-active-test",),
                actor_id=worker_id,
                key_id=key.key_id,
            )
        )
    store.close()

    start_at = time.time() + 0.5
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CLAIM_SCRIPT,
                db_path,
                contract_id,
                str(replica),
                str(start_at),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for replica in range(replica_count)
    ]
    claims = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        claims.append(stdout.strip())

    granted = [claim_id for claim_id in claims if claim_id]
    assert len(granted) == 1
    reopened = SQLiteStore(db_path)
    records = reopened.scan_runtime_records(record_type="claim.granted")
    assert len(records) == 1
    assert records[0][1].payload["claim_id"] == granted[0]
    reopened.close()
