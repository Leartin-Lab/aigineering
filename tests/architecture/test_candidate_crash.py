"""Process-level crash atomicity for the Candidate commitment boundary."""

from __future__ import annotations

import os
import subprocess
import sys

from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.types import Contract
from conftest import candidate_runtime


def _run_with_crash(crash_point: str, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "AIG_ENABLE_CRASH_INJECTION": "1",
            "AIG_CRASH_POINT": crash_point,
        },
        capture_output=True,
        text=True,
    )


def test_candidate_crash_after_asset_before_trace_rolls_back_everything(tmp_path):
    path = tmp_path / "candidate-crash.db"
    store = SQLiteStore(str(path))
    setup_signer = Ed25519Signer()
    setup_actor = ActorKey(
        "setup",
        "setup-key",
        setup_signer.kind,
        setup_signer.signer_id,
        ("contract.publish",),
    )
    genesis = create_genesis_manifest(
        "crash-domain",
        [
            setup_actor,
            ActorKey("actor", "key", "crash-test", "public", ("asset.publish",)),
        ],
        "policy:test",
    )
    initialize_genesis(store, genesis)
    fields = {
        "name": "crash_target",
        "description": "",
        "inputs": (),
        "outputs": ("result",),
        "activation": "",
        "budget": 1,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    candidate_runtime(
        store,
        genesis=genesis,
        actor_key=setup_actor,
        signer=setup_signer,
    ).accept_contract(contract)
    before_revision = store.get_runtime_revision()
    store.close()

    script = f"""
import hashlib
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.signing import Signer, Verifier
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import CandidateEffect, create_candidate_proposal

class CrashSigner(Signer):
    kind = "crash-test"
    signer_id = "public"
    def sign(self, data):
        return hashlib.sha256(self.signer_id.encode() + data).hexdigest()

class CrashVerifier(Verifier):
    def verify(self, data, signature, signer_id):
        return signature == hashlib.sha256(signer_id.encode() + data).hexdigest()

store = SQLiteStore({str(path)!r})
genesis = __import__("aigineering.core.domain", fromlist=["load_genesis"]).load_genesis(store)
candidate = create_candidate_proposal(
    domain_id=genesis.id,
    actor_id="actor",
    key_id="key",
    effects=[CandidateEffect("asset.propose", {{"asset": {{"name": "result", "content": "partial"}}}})],
    signer=CrashSigner(),
)
CandidateCommitter(store, store).commit(
    candidate,
    verifier_factory=lambda kind: CrashVerifier(),
)
"""
    crashed = _run_with_crash("after_asset_before_trace", script)
    assert crashed.returncode == 1

    reopened = SQLiteStore(str(path))
    assert reopened.get_assets_by_name("result") == []
    assert reopened.get_runtime_revision() == before_revision
    assert not any(
        record.payload.get("actor_id") == "actor"
        for _, record in reopened.scan_runtime_records(record_type="candidate.received")
    )
    assert reopened.scan_runtime_records(record_type="lifecycle.terminal") == []
    reopened.close()


def test_terminal_crash_before_claim_release_rolls_back_both_facts(tmp_path):
    path = tmp_path / "terminal-claim-crash.db"
    store = SQLiteStore(str(path))
    contract = Contract(id="task:terminal-claim-crash", name="crash")
    store.add_contract(contract)
    claim = store.claim_contract(
        contract.id,
        "worker:crash",
        package_id="package:crash",
    )
    assert claim is not None
    before_revision = store.get_runtime_revision()
    store.close()

    script = f"""
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.runtime_record import create_runtime_record

store = SQLiteStore({str(path)!r})
terminal = create_runtime_record(
    "lifecycle.terminal",
    {{"contract_id": {contract.id!r}, "terminal": "cancelled"}},
)
store.commit_ingress_batch(
    accepted_assets=[],
    trace_entries=[],
    runtime_records=(terminal,),
)
"""
    crashed = _run_with_crash("after_terminal_before_claim_release", script)
    assert crashed.returncode == 1

    reopened = SQLiteStore(str(path))
    assert reopened.get_runtime_revision() == before_revision
    assert reopened.scan_runtime_records(record_type="lifecycle.terminal") == []
    assert reopened.scan_runtime_records(record_type="claim.released") == []
    rebuilt = reopened.get_claim(contract.id)
    assert rebuilt is not None and rebuilt["status"] == "active"
    reopened.rebuild_claim_projection()
    rebuilt = reopened.get_claim(contract.id)
    assert rebuilt is not None and rebuilt["status"] == "active"
    reopened.close()
