from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

from aigineering.core.integrity import check_store_integrity
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import ActorKey
from conftest import candidate_runtime


class _Worker:
    worker_id = "worker:materialization-identity"

    def registration(self):
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        raise AssertionError("registration fixture does not invoke a Worker")


def _record_ids(store):
    return tuple(
        (record.id, record.record_type, record.recorded_at)
        for _, record in store.scan_runtime_records()
    )


def _signed_store(path):
    store = SQLiteStore(str(path))
    setup = candidate_runtime(store)
    worker_signer = Ed25519Signer()
    worker_key = ActorKey(
        _Worker.worker_id,
        "key:materialization-identity",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    setup.authorize_actor(worker_key)
    setup.register_worker(
        WorkerRegistration(
            _Worker.worker_id,
            actor_id=worker_key.actor_id,
            key_id=worker_key.key_id,
            capacity=2,
            version="signed-registration-v1",
        )
    )
    registration = store.get_worker_registration(_Worker.worker_id)
    assert registration is not None
    assert registration.actor_id
    assert registration.key_id
    return store, registration


@pytest.mark.parametrize(
    ("column", "field"),
    (("actor_id", "actor_id"), ("key_id", "key_id")),
)
def test_registration_identity_projection_is_integrity_covered_and_rebuildable(
    tmp_path, column, field
):
    path = tmp_path / f"registration-{field}.sqlite"
    store, original = _signed_store(path)
    try:
        original_digest = store.runtime_materialization_digest()
        original_records = _record_ids(store)
        assert check_store_integrity(store)["consistent"] is True

        with sqlite3.connect(path) as connection:
            connection.execute(
                f"UPDATE worker_registrations SET {column} = ? WHERE worker_id = ?",
                (f"tampered:{field}", original.worker_id),
            )

        assert _record_ids(store) == original_records
        assert store.runtime_materialization_digest() != original_digest
        report = check_store_integrity(store)
        assert report["consistent"] is False
        assert report["materializations_match"] is False
        # Integrity checks rebuild their private snapshot and do not repair the
        # caller's projected row or append/replace signed facts.
        corrupted = store.get_worker_registration(original.worker_id)
        assert getattr(corrupted, field) == f"tampered:{field}"
        assert replace(corrupted, **{field: getattr(original, field)}) == original
        assert _record_ids(store) == original_records

        rebuilt_digest = store.rebuild_runtime_materializations()
        assert rebuilt_digest == original_digest
        assert getattr(store.get_worker_registration(original.worker_id), field) == (
            getattr(original, field)
        )
        assert _record_ids(store) == original_records
        assert check_store_integrity(store)["consistent"] is True
    finally:
        store.close()

    reopened = SQLiteStore(str(path))
    try:
        assert reopened.runtime_materialization_digest() == original_digest
        assert check_store_integrity(reopened)["consistent"] is True
        assert _record_ids(reopened) == original_records
    finally:
        reopened.close()
