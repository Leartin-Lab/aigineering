"""Signed Worker coordination is revalidated at the SQLite writer."""

from __future__ import annotations

from threading import Barrier, Thread

import pytest

from aigineering.agent.harness import HarnessCandidateAdapter
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_coordination import authenticate_worker_command
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    actor_revocation_effect,
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package


def _world(db_path: str, *, publish_task: bool = True):
    store = SQLiteStore(db_path)
    admin_signer = Ed25519Signer()
    admin = ActorKey(
        "test:worker-auth-admin",
        "admin-key",
        admin_signer.kind,
        admin_signer.signer_id,
        ("actor.authorize", "actor.revoke", "worker.register", "contract.publish"),
    )
    genesis = create_genesis_manifest(
        "worker-auth-test", (admin,), "policy:worker-auth-test"
    )
    initialize_genesis(store, genesis)
    admin_publisher = CandidatePublisher(store, store, genesis, admin, admin_signer)
    worker_signer = Ed25519Signer()
    worker_key = ActorKey(
        "worker:auth-test",
        "worker-key-1",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    for effect, key in (
        (actor_authorization_effect(worker_key), "worker-key"),
        (
            worker_registration_effect(
                WorkerRegistration(
                    worker_key.actor_id,
                    capacity=2,
                    version="1",
                    actor_id=worker_key.actor_id,
                    key_id=worker_key.key_id,
                )
            ),
            "registration-1",
        ),
    ):
        decision = admin_publisher.publish((effect,), idempotency_key=key)
        assert decision.accepted
    contract = _publish_contract(admin_publisher, "first") if publish_task else None
    adapter = HarnessCandidateAdapter(genesis.id, worker_key, worker_signer)
    return (
        store,
        admin,
        admin_signer,
        genesis,
        admin_publisher,
        worker_key,
        adapter,
        contract,
    )


def _publish_contract(publisher, name: str) -> Contract:
    fields = {
        "name": name,
        "description": f"Work for {name}.",
        "inputs": (),
        "outputs": (f"{name}_result",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    result = publisher.publish(
        (contract_declaration_effect(contract),), idempotency_key=f"publish:{name}"
    )
    assert result.accepted
    return contract


def test_claim_candidate_is_single_use_even_when_a_second_task_appears(tmp_path):
    store, _admin, _signer, _genesis, publisher, worker_key, adapter, contract = _world(
        str(tmp_path / "claim-replay.db")
    )
    try:
        request = adapter.claim_candidate(request_id="claim-replay", contract_id=None)
        first = claim_next_package(
            store,
            worker_id=worker_key.actor_id,
            claim_candidate=request,
        )
        assert first is not None
        second_task = _publish_contract(publisher, "second")
        before = tuple(record.id for _, record in store.scan_runtime_records())

        replay = claim_next_package(
            store,
            worker_id=worker_key.actor_id,
            claim_candidate=request,
        )

        assert replay is None
        assert store.get_claim(second_task.id) is None
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
    finally:
        store.close()


def test_null_target_no_task_scan_is_retryable_without_recording_request(tmp_path):
    store, _admin, _signer, _genesis, publisher, worker_key, adapter, _contract = (
        _world(str(tmp_path / "null-scan.db"), publish_task=False)
    )
    try:
        request = adapter.claim_candidate(request_id="claim-null-retry")
        before = tuple(record.id for _, record in store.scan_runtime_records())
        assert (
            claim_next_package(
                store, worker_id=worker_key.actor_id, claim_candidate=request
            )
            is None
        )
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before

        contract = _publish_contract(publisher, "created-later")
        claimed = claim_next_package(
            store, worker_id=worker_key.actor_id, claim_candidate=request
        )
        assert claimed is not None
        assert claimed.contract.id == contract.id
        request_record = store.scan_runtime_records(
            record_type="worker.claim.requested"
        )[0][1]
        receipt_record = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.id in request_record.causal_parents
        )
        grant_record = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload["contract_id"] == contract.id
        )
        assert request_record.causal_parents == (receipt_record.id,)
        assert grant_record.causal_parents == (request_record.id,)
        before_rebuild = store.get_claim(contract.id)
        store.rebuild_claim_projection()
        assert store.get_claim(contract.id) == before_rebuild
    finally:
        store.close()


def test_claim_rejects_registration_rotation_after_preflight_and_rolls_back(tmp_path):
    db_path = str(tmp_path / "registration-rotation.db")
    store, admin, admin_signer, genesis, _publisher, worker_key, adapter, contract = (
        _world(db_path)
    )
    second = SQLiteStore(db_path)
    try:
        command_candidate = adapter.claim_candidate(
            request_id="claim-before-rotation", contract_id=contract.id
        )
        command = authenticate_worker_command(command_candidate, "worker.claim", store)
        barrier = Barrier(2)
        outcomes: list[object] = []

        def rotate_registration() -> None:
            publisher = CandidatePublisher(second, second, genesis, admin, admin_signer)
            barrier.wait()
            replacement_signer = Ed25519Signer()
            replacement_key = ActorKey(
                worker_key.actor_id,
                "worker-key-2",
                replacement_signer.kind,
                replacement_signer.signer_id,
                ("worker.submit",),
            )
            result = publisher.publish(
                (
                    actor_authorization_effect(replacement_key),
                    worker_registration_effect(
                        WorkerRegistration(
                            worker_key.actor_id,
                            capacity=2,
                            version="2",
                            actor_id=worker_key.actor_id,
                            key_id="worker-key-2",
                        )
                    ),
                ),
                idempotency_key="registration-rotation-2",
            )
            outcomes.append(result.accepted)

        thread = Thread(target=rotate_registration)
        thread.start()
        barrier.wait()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert outcomes == [True]
        old_key = next(
            item
            for item in load_effective_actor_keys(store, genesis)
            if (item.actor_id, item.key_id) == (worker_key.actor_id, worker_key.key_id)
        )
        assert old_key.revoked is False
        before = tuple(record.id for _, record in store.scan_runtime_records())

        with pytest.raises(ValueError, match="enabled actor-key registration"):
            store.claim_contract(
                contract.id,
                worker_key.actor_id,
                package_id="pkg:rotation-race",
                expected_registration_version="1",
                runtime_records=command.runtime_records,
                claim_candidate=command_candidate,
            )

        assert store._conn.in_transaction is False
        assert store.get_claim(contract.id) is None
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
        assert store.claim_contract(contract.id, worker_key.actor_id) is not None
    finally:
        second.close()
        store.close()


def test_claim_rejects_revoked_key_after_preflight_without_partial_facts(tmp_path):
    db_path = str(tmp_path / "revocation-race.db")
    store, admin, admin_signer, genesis, _publisher, worker_key, adapter, contract = (
        _world(db_path)
    )
    second = SQLiteStore(db_path)
    try:
        candidate = adapter.claim_candidate(
            request_id="claim-before-revoke", contract_id=contract.id
        )
        command = authenticate_worker_command(candidate, "worker.claim", store)
        publisher = CandidatePublisher(second, second, genesis, admin, admin_signer)
        decision = publisher.publish(
            (
                actor_revocation_effect(
                    worker_key.actor_id, worker_key.key_id, "rotation test"
                ),
            ),
            idempotency_key="revoke-worker-key",
        )
        assert decision.accepted
        before = tuple(record.id for _, record in store.scan_runtime_records())

        with pytest.raises(ValueError, match="authorized|revoked"):
            store.claim_contract(
                contract.id,
                worker_key.actor_id,
                runtime_records=command.runtime_records,
                claim_candidate=candidate,
            )

        assert store._conn.in_transaction is False
        assert store.get_claim(contract.id) is None
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
        assert store.claim_contract(contract.id, worker_key.actor_id) is not None
    finally:
        second.close()
        store.close()


def test_claim_registration_version_is_rechecked_at_writer(tmp_path):
    db_path = str(tmp_path / "registration-version.db")
    store, admin, admin_signer, genesis, _publisher, worker_key, adapter, contract = (
        _world(db_path)
    )
    second = SQLiteStore(db_path)
    try:
        candidate = adapter.claim_candidate(
            request_id="claim-before-version", contract_id=contract.id
        )
        command = authenticate_worker_command(candidate, "worker.claim", store)
        publisher = CandidatePublisher(second, second, genesis, admin, admin_signer)
        result = publisher.publish(
            (
                worker_registration_effect(
                    WorkerRegistration(
                        worker_key.actor_id,
                        capacity=2,
                        version="2",
                        actor_id=worker_key.actor_id,
                        key_id=worker_key.key_id,
                    )
                ),
            ),
            idempotency_key="registration-version-2",
        )
        assert result.accepted
        before = tuple(record.id for _, record in store.scan_runtime_records())
        with pytest.raises(ValueError, match="registration changed"):
            store.claim_contract(
                contract.id,
                worker_key.actor_id,
                expected_registration_version="1",
                runtime_records=command.runtime_records,
                claim_candidate=candidate,
            )
        assert store._conn.in_transaction is False
        assert store.get_claim(contract.id) is None
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
    finally:
        second.close()
        store.close()


def test_disabled_registration_after_preflight_rejects_claim(tmp_path):
    db_path = str(tmp_path / "disabled-registration.db")
    store, admin, admin_signer, genesis, _publisher, worker_key, adapter, contract = (
        _world(db_path)
    )
    second = SQLiteStore(db_path)
    try:
        candidate = adapter.claim_candidate(
            request_id="claim-before-disable", contract_id=contract.id
        )
        command = authenticate_worker_command(candidate, "worker.claim", store)
        publisher = CandidatePublisher(second, second, genesis, admin, admin_signer)
        disabled = publisher.publish(
            (
                worker_registration_effect(
                    WorkerRegistration(
                        worker_key.actor_id,
                        capacity=2,
                        enabled=False,
                        version="2",
                        actor_id=worker_key.actor_id,
                        key_id=worker_key.key_id,
                    )
                ),
            ),
            idempotency_key="disable-worker",
        )
        assert disabled.accepted
        before = tuple(record.id for _, record in store.scan_runtime_records())
        with pytest.raises(ValueError, match="enabled actor-key registration"):
            store.claim_contract(
                contract.id,
                worker_key.actor_id,
                runtime_records=command.runtime_records,
                claim_candidate=candidate,
            )
        assert store._conn.in_transaction is False
        assert store.get_claim(contract.id) is None
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
    finally:
        second.close()
        store.close()


def test_renewal_rechecks_registration_and_replay_does_not_extend_twice(tmp_path):
    db_path = str(tmp_path / "renewal-auth.db")
    store, admin, admin_signer, genesis, _publisher, worker_key, adapter, contract = (
        _world(db_path)
    )
    second = SQLiteStore(db_path)
    try:
        claim_candidate = adapter.claim_candidate(
            request_id="renewal-claim", contract_id=contract.id, lease_seconds=60
        )
        claimed = claim_next_package(
            store,
            worker_id=worker_key.actor_id,
            contract_id=contract.id,
            claim_candidate=claim_candidate,
        )
        assert claimed is not None
        renewal = adapter.renewal_candidate(
            claimed.package, request_id="renewal-once", lease_seconds=90
        )
        command = authenticate_worker_command(renewal, "worker.claim.renew", store)
        renewed = store.renew_claim(
            claimed.package.claim_id,
            claimed.package.claim_epoch,
            worker_key.actor_id,
            lease_seconds=90,
            runtime_records=command.runtime_records,
            claim_candidate=renewal,
        )
        assert renewed is not None
        lease_after_first = renewed["lease_until"]
        renewal_request = store.scan_runtime_records(
            record_type="worker.claim.renew.requested"
        )[0][1]
        renewal_receipt = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.id in renewal_request.causal_parents
        )
        grant_record = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload["claim_id"] == claimed.package.claim_id
        )
        renewed_record = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.renewed")
            if record.payload["claim_id"] == claimed.package.claim_id
        )
        assert renewal_request.causal_parents == (renewal_receipt.id,)
        assert renewed_record.causal_parents == (grant_record.id, renewal_request.id)
        before_rebuild = store.get_claim(contract.id)
        store.rebuild_claim_projection()
        assert store.get_claim(contract.id) == before_rebuild
        fact_ids_after_first = tuple(
            record.id for _, record in store.scan_runtime_records()
        )
        assert (
            store.renew_claim(
                claimed.package.claim_id,
                claimed.package.claim_epoch,
                worker_key.actor_id,
                lease_seconds=90,
                runtime_records=command.runtime_records,
                claim_candidate=renewal,
            )
            is None
        )
        assert store.get_claim(contract.id)["lease_until"] == lease_after_first
        assert tuple(record.id for _, record in store.scan_runtime_records()) == (
            fact_ids_after_first
        )

        changed_renewal = adapter.renewal_candidate(
            claimed.package, request_id="renewal-after-rotation", lease_seconds=90
        )
        publisher = CandidatePublisher(second, second, genesis, admin, admin_signer)
        replacement_signer = Ed25519Signer()
        replacement_key = ActorKey(
            worker_key.actor_id,
            "worker-key-2",
            replacement_signer.kind,
            replacement_signer.signer_id,
            ("worker.submit",),
        )
        rotated = publisher.publish(
            (
                actor_authorization_effect(replacement_key),
                worker_registration_effect(
                    WorkerRegistration(
                        worker_key.actor_id,
                        capacity=2,
                        version="2",
                        actor_id=worker_key.actor_id,
                        key_id="worker-key-2",
                    )
                ),
            ),
            idempotency_key="renewal-registration-2",
        )
        assert rotated.accepted
        before = tuple(record.id for _, record in store.scan_runtime_records())
        with pytest.raises(ValueError, match="enabled actor-key registration"):
            store.renew_claim(
                claimed.package.claim_id,
                claimed.package.claim_epoch,
                worker_key.actor_id,
                lease_seconds=90,
                claim_candidate=changed_renewal,
            )
        assert store._conn.in_transaction is False
        assert store.get_claim(contract.id)["lease_until"] == lease_after_first
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
    finally:
        second.close()
        store.close()


def test_store_rejects_altered_command_record_preview(tmp_path):
    store, _admin, _signer, _genesis, _publisher, worker_key, adapter, contract = (
        _world(str(tmp_path / "altered-preview.db"))
    )
    try:
        candidate = adapter.claim_candidate(
            request_id="altered-preview", contract_id=contract.id
        )
        command = authenticate_worker_command(candidate, "worker.claim", store)
        forged = create_runtime_record(
            "worker.claim.requested",
            {
                "candidate_id": candidate.id,
                "idempotency_key": candidate.idempotency_key,
                "key_id": candidate.key_id,
                "worker_id": worker_key.actor_id,
                "contract_id": contract.id,
                "lease_seconds": 60,
                "extra": "changed",
            },
            causal_parents=(command.runtime_records[0].id,),
        )
        before = tuple(record.id for _, record in store.scan_runtime_records())

        result = store.claim_contract(
            contract.id,
            worker_key.actor_id,
            runtime_records=(command.runtime_records[0], forged),
            claim_candidate=candidate,
        )

        assert result is None
        assert store.get_claim(contract.id) is None
        assert tuple(record.id for _, record in store.scan_runtime_records()) == before
    finally:
        store.close()


def test_store_rejects_no_candidate_with_supplied_operational_records(tmp_path):
    store, _admin, _signer, _genesis, _publisher, worker_key, adapter, contract = (
        _world(str(tmp_path / "unsigned-records.db"))
    )
    try:
        candidate = adapter.claim_candidate(
            request_id="unsigned-records", contract_id=contract.id
        )
        command = authenticate_worker_command(candidate, "worker.claim", store)
        with pytest.raises(ValueError, match="require their signed claim_candidate"):
            store.claim_contract(
                contract.id,
                worker_key.actor_id,
                runtime_records=command.runtime_records,
            )
        assert store.get_claim(contract.id) is None
        assert not store._conn.in_transaction
    finally:
        store.close()


def test_command_parser_rejects_extra_fields_and_boolean_numbers(tmp_path):
    (
        store,
        _admin,
        _admin_signer,
        genesis,
        _publisher,
        worker_key,
        _adapter,
        _contract,
    ) = _world(str(tmp_path / "strict-command.db"))
    try:
        # Construct a signed malformed payload with the already-authorized
        # Worker key material held by the fixture adapter.
        adapter = _adapter
        malformed = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=adapter.actor_key.actor_id,
            key_id=adapter.actor_key.key_id,
            effects=(
                CandidateEffect(
                    "worker.claim",
                    {
                        "worker_id": adapter.worker_id,
                        "contract_id": None,
                        "lease_seconds": True,
                        "candidate_id": "forged-provenance",
                    },
                ),
            ),
            signer=adapter.signer,
            idempotency_key="malformed-command",
        )
        del worker_key
        with pytest.raises(ValueError, match="invalid fields"):
            authenticate_worker_command(malformed, "worker.claim", store)
    finally:
        store.close()


def test_recordless_trusted_claim_and_renewal_keep_legacy_fact_shapes(tmp_path):
    store, _admin, _signer, _genesis, _publisher, worker_key, _adapter, contract = (
        _world(str(tmp_path / "recordless-legacy-shape.db"))
    )
    try:
        claim = store.claim_contract(contract.id, worker_key.actor_id)
        assert claim is not None
        grant = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload["claim_id"] == claim["claim_id"]
        )
        assert grant.causal_parents == ()
        assert set(grant.payload) == {
            "claim_id",
            "contract_id",
            "epoch",
            "lease_until",
            "package_id",
            "worker_id",
        }
        assert not store.scan_runtime_records(record_type="worker.claim.requested")

        renewed = store.renew_claim(
            claim["claim_id"],
            claim["epoch"],
            worker_key.actor_id,
            lease_seconds=120,
        )
        assert renewed is not None
        renewal = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.renewed")
            if record.payload["claim_id"] == claim["claim_id"]
        )
        assert renewal.causal_parents == (grant.id,)
        assert set(renewal.payload) == {
            "claim_id",
            "epoch",
            "lease_until",
            "worker_id",
        }
        assert not store.scan_runtime_records(
            record_type="worker.claim.renew.requested"
        )
        before_rebuild = store.get_claim(contract.id)
        store.rebuild_claim_projection()
        assert store.get_claim(contract.id) == before_rebuild
    finally:
        store.close()
