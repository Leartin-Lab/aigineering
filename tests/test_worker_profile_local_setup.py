from __future__ import annotations


import pytest

from aigineering.agent.llm import LLMWorker
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import load_genesis
from aigineering.core.ids import compute_content_hash
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_worker_host,
    load_actor_signer,
    actor_key_path,
)
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    worker_registration_effect,
)


class _PullOnlyWorker:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id

    def registration(self):
        return WorkerRegistration(self.worker_id)


@pytest.mark.parametrize("enabled", [False, True])
def test_local_setup_does_not_rebind_external_or_disabled_worker(
    tmp_path, monkeypatch, enabled
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    store = SQLiteStore(".aig/store.db")
    worker_id = "worker:external-registration"
    try:
        ensure_local_domain(store)
        genesis = load_genesis(store)
        root_signer = load_actor_signer(actor_key_path())
        root_key = next(
            key for key in genesis.root_keys if key.public_key == root_signer.signer_id
        )
        external_signer = Ed25519Signer.from_private_key_hex("55" * 32)
        external_key = ActorKey(
            worker_id,
            "externally-managed-key",
            external_signer.kind,
            external_signer.signer_id,
            ("worker.submit",),
        )
        external = WorkerRegistration(
            worker_id,
            enabled=enabled,
            version="7",
            profile_id="external-managed-v7",
            actor_id=worker_id,
            key_id="externally-managed-key",
        )
        publisher = CandidatePublisher(store, store, genesis, root_key, root_signer)
        decision = publisher.publish(
            (
                actor_authorization_effect(external_key),
                worker_registration_effect(external),
            ),
            idempotency_key="external-worker-fixture",
        )
        assert decision.accepted
        before = tuple(record.id for _, record in store.scan_runtime_records())
        signer_path = actor_key_path().parent / (
            f"worker-{compute_content_hash(worker_id)[:16]}.ed25519"
        )
        assert not signer_path.exists()
        llm = LLMWorker(
            model="must-not-provision-external",
            api_key="explicit-non-provider-test-value",
            worker_id=worker_id,
        )
        try:
            ensure_local_worker_host(store, llm)
        except ValueError as exc:
            if enabled:
                assert "not the canonical local signer" in str(exc)
            else:
                assert "no enabled local Worker registration" in str(exc)
        else:
            raise AssertionError("external registration must not be rebound")
        assert not signer_path.exists()
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
        current = store.get_worker_registration(worker_id)
        assert current is not None
        assert current.version == "7"
        assert current.key_id == "externally-managed-key"
    finally:
        store.close()
