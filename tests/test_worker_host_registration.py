from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread

import pytest

from aigineering.agent.worker import WorkerHost
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package
from conftest import hosted_worker


class _Worker:
    worker_id = "worker:registration-version"

    def registration(self):
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        raise AssertionError("claim preparation must not invoke a Worker")


def _world(db_path):
    store = SQLiteStore(str(db_path))
    worker = _Worker()
    host = hosted_worker(store, worker)
    registration = store.get_worker_registration(worker.worker_id)
    assert registration is not None
    host = replace(host, registration_version=registration.version)
    contract = Contract(
        id="task:registration-version",
        name="registration version target",
        outputs=("result",),
        budget=1,
    )
    store.add_contract(contract)
    return store, worker, host, registration, contract


def test_worker_host_registration_version_defaults_to_unbound_compatibility():
    store = SQLiteStore(":memory:")
    worker = _Worker()
    authorized = hosted_worker(store, worker)
    host = WorkerHost(
        authorized.worker,
        authorized.genesis,
        authorized.actor_key,
        authorized.signer,
    )
    assert host.registration_version == ""
    store.close()


def test_previously_created_stale_host_is_refused_before_maintenance(
    tmp_path, monkeypatch
):
    store, worker, host, registration, contract = _world(
        tmp_path / "stale-worker-host.db"
    )
    try:
        store.register_worker(
            replace(registration, version="2", profile_id="openai-compatible-v1")
        )
        maintenance_calls = []
        monkeypatch.setattr(
            "aigineering.runtime.process_expired_claims",
            lambda *args, **kwargs: maintenance_calls.append((args, kwargs)) or [],
        )

        with pytest.raises(ValueError, match="WorkerHost registration version.*stale"):
            claim_next_package(
                store,
                worker_id=host.worker_id,
                contract_id=contract.id,
                expected_registration_version=host.registration_version,
                claim_candidate=host.claim_candidate(
                    request_id="stale-host-claim",
                    contract_id=contract.id,
                ),
            )

        assert not maintenance_calls
        assert store.get_claim(contract.id) is None

        current_host = replace(host, registration_version="2")
        claimed = claim_next_package(
            store,
            worker_id=current_host.worker_id,
            contract_id=contract.id,
            expected_registration_version=current_host.registration_version,
            claim_candidate=current_host.claim_candidate(
                request_id="current-host-claim",
                contract_id=contract.id,
            ),
        )
        assert claimed is not None
        assert claimed.package.worker_registration_version == "2"
    finally:
        store.close()


def test_registration_change_after_routing_preflight_is_writer_fenced(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "claim-promotion-race.db"
    store, worker, host, registration, contract = _world(db_path)
    competitor = SQLiteStore(str(db_path))
    reached_writer = Event()
    release_writer = Event()
    original_claim = store.claim_contract
    claim_errors = []
    claim_results = []

    def pause_before_claim_writer(*args, **kwargs):
        reached_writer.set()
        assert release_writer.wait(timeout=5)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim_contract", pause_before_claim_writer)
    monkeypatch.setattr(
        "aigineering.runtime.process_expired_claims", lambda *args, **kwargs: []
    )

    def select_and_claim():
        try:
            claim_results.append(
                claim_next_package(
                    store,
                    worker_id=host.worker_id,
                    contract_id=contract.id,
                    expected_registration_version=host.registration_version,
                    claim_candidate=host.claim_candidate(
                        request_id="claim-before-promotion-writer",
                        contract_id=contract.id,
                    ),
                )
            )
        except Exception as exc:  # captured so a thread failure cannot hide
            claim_errors.append(exc)

    thread = Thread(target=select_and_claim)
    thread.start()
    try:
        assert reached_writer.wait(timeout=5)
        competitor.register_worker(
            replace(registration, version="2", profile_id="openai-compatible-v1")
        )
        release_writer.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(claim_errors) == 1
        assert isinstance(claim_errors[0], ValueError)
        assert "worker registration changed after package selection" in str(
            claim_errors[0]
        )
        assert claim_results == []
        assert store.get_claim(contract.id) is None
        assert competitor.get_worker_registration(worker.worker_id).version == "2"
    finally:
        release_writer.set()
        thread.join(timeout=5)
        competitor.close()
        store.close()
