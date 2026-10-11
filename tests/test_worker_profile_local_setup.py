from __future__ import annotations

import json
from dataclasses import replace

import pytest

from aigineering.agent.llm import LLMWorker
from aigineering.cli.main import cli
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
    actor_revocation_effect,
)
from click.testing import CliRunner


class _PullOnlyWorker:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id

    def registration(self):
        return WorkerRegistration(self.worker_id)


def test_local_worker_setup_promotes_once_and_discovery_returns_history(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    store = SQLiteStore(".aig/store.db")
    worker_id = "worker:profile-local-setup"
    try:
        ensure_local_domain(store)
        pull_host = ensure_local_worker_host(store, _PullOnlyWorker(worker_id))
        assert pull_host.registration_version == "1"
        before = tuple(record.id for _, record in store.scan_runtime_records())

        llm = LLMWorker(
            model="profile-setup-no-invocation",
            api_key="explicit-non-provider-test-value",
            base_url="http://127.0.0.1:9/v1",
            worker_id=worker_id,
        )
        concrete_host = ensure_local_worker_host(store, llm)
        promoted_ids = tuple(record.id for _, record in store.scan_runtime_records())
        assert concrete_host.registration_version == "2"
        assert promoted_ids != before
        promotion_facts = store.scan_runtime_records(
            record_type="worker.profile.promoted"
        )
        assert len(promotion_facts) == 1
        current = store.get_worker_registration(worker_id)
        assert current is not None
        assert current.profile_id == "openai-compatible-v1"
        assert current.version == concrete_host.registration_version

        repeated_host = ensure_local_worker_host(store, llm)
        assert repeated_host.registration_version == "2"
        assert (
            tuple(record.id for _, record in store.scan_runtime_records())
            == promoted_ids
        )

        changed_routing = LLMWorker(
            model="profile-setup-no-invocation",
            api_key="explicit-non-provider-test-value",
            base_url="http://127.0.0.1:9/v1",
            worker_id=worker_id,
            routing_capabilities=frozenset({"changed-route"}),
        )
        try:
            ensure_local_worker_host(store, changed_routing)
        except ValueError as exc:
            assert "does not match this adapter" in str(exc)
        else:
            raise AssertionError("routing changes require explicit registration")
        assert (
            tuple(record.id for _, record in store.scan_runtime_records())
            == promoted_ids
        )
    finally:
        store.close()

    result = CliRunner().invoke(cli, ["worker", "registrations", worker_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["current"]["version"] == "2"
    assert payload["data"]["current"]["profile_id"] == "openai-compatible-v1"
    assert len(payload["data"]["history"]) == 2
    assert len(payload["data"]["promotions"]) == 1
    assert payload["data"]["promotions"][0]["record_id"]


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


def test_local_worker_can_share_root_actor_with_a_distinct_signing_key(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    store = SQLiteStore(".aig/store.db")
    worker_id = "human:owner"
    try:
        ensure_local_domain(store)
        pull_host = ensure_local_worker_host(store, _PullOnlyWorker(worker_id))
        root = load_genesis(store).root_keys[0]
        assert root.actor_id == worker_id
        assert pull_host.actor_key.actor_id == root.actor_id
        assert pull_host.actor_key.key_id != root.key_id

        llm = LLMWorker(
            model="same-actor-no-provider-call",
            api_key="explicit-non-provider-test-value",
            base_url="http://127.0.0.1:9/v1",
            worker_id=worker_id,
        )
        host = ensure_local_worker_host(store, llm)
        assert host.registration_version == "2"
        promoted = store.scan_runtime_records(record_type="worker.profile.promoted")
        assert len(promoted) == 1
        promotion = promoted[0][1]
        receipt = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("candidate_id") == promotion.payload["candidate_id"]
        )
        assert receipt.payload["actor_id"] == promotion.payload["actor_id"]
        assert receipt.payload["key_id"] != promotion.payload["key_id"]
        before = tuple(record.id for _, record in store.scan_runtime_records())
        assert ensure_local_worker_host(store, llm).registration_version == "2"
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
    finally:
        store.close()


@pytest.mark.parametrize("condition", ["missing-signer", "revoked-signer"])
def test_local_profile_promotion_refuses_missing_or_revoked_signer(
    tmp_path, monkeypatch, condition
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    store = SQLiteStore(".aig/store.db")
    worker_id = f"worker:{condition}"
    try:
        ensure_local_domain(store)
        ensure_local_worker_host(store, _PullOnlyWorker(worker_id))
        genesis = load_genesis(store)
        current = store.get_worker_registration(worker_id)
        assert current is not None
        worker_path = actor_key_path().parent / f"{current.key_id}.ed25519"
        root_signer = load_actor_signer(actor_key_path())
        root_key = next(
            key for key in genesis.root_keys if key.public_key == root_signer.signer_id
        )
        authority = CandidatePublisher(store, store, genesis, root_key, root_signer)
        if condition == "missing-signer":
            worker_path.unlink()
        else:
            result = authority.publish(
                (
                    actor_revocation_effect(
                        current.actor_id, current.key_id, "revoke local profile signer"
                    ),
                ),
                idempotency_key="revoke-local-profile-signer",
            )
            assert result.accepted
        before = tuple(record.id for _, record in store.scan_runtime_records())
        llm = LLMWorker(
            model="must-not-promote",
            api_key="explicit-non-provider-test-value",
            worker_id=worker_id,
        )
        with pytest.raises(ValueError, match="signer|authorization"):
            ensure_local_worker_host(store, llm)
        if condition == "missing-signer":
            assert not worker_path.exists()
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
        assert not store.scan_runtime_records(record_type="worker.profile.promoted")
    finally:
        store.close()


def test_local_profile_promotion_refuses_multiple_registration_history(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    store = SQLiteStore(".aig/store.db")
    worker_id = "worker:multiple-registration-history"
    try:
        ensure_local_domain(store)
        ensure_local_worker_host(store, _PullOnlyWorker(worker_id))
        genesis = load_genesis(store)
        current = store.get_worker_registration(worker_id)
        assert current is not None
        root_signer = load_actor_signer(actor_key_path())
        root_key = next(
            key for key in genesis.root_keys if key.public_key == root_signer.signer_id
        )
        authority = CandidatePublisher(store, store, genesis, root_key, root_signer)
        second = replace(current, version="2")
        assert authority.publish(
            (worker_registration_effect(second),),
            idempotency_key="explicit-second-local-registration",
        ).accepted
        before = tuple(record.id for _, record in store.scan_runtime_records())
        llm = LLMWorker(
            model="must-not-promote-history",
            api_key="explicit-non-provider-test-value",
            worker_id=worker_id,
        )
        with pytest.raises(ValueError, match="does not match this adapter"):
            ensure_local_worker_host(store, llm)
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
        assert not store.scan_runtime_records(record_type="worker.profile.promoted")
    finally:
        store.close()
