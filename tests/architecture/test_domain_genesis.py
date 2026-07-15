"""Store conformance for durable, unique Genesis bootstrap."""

from __future__ import annotations

import pytest

from aigineering.core.domain import initialize_genesis, load_genesis
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.runtime_record import create_runtime_record


def _manifest(domain: str = "domain-a"):
    return create_genesis_manifest(
        domain,
        [ActorKey("owner", "root", "ed25519", "00" * 32, ("contract.publish",))],
        "policy:test",
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    value = MemoryStore() if request.param == "memory" else SQLiteStore(":memory:")
    yield value
    if isinstance(value, SQLiteStore):
        value.close()


def test_genesis_initialization_is_idempotent_and_reconstructable(store):
    manifest = _manifest()

    assert initialize_genesis(store, manifest) == manifest
    revision = store.get_runtime_revision()
    assert initialize_genesis(store, manifest) == manifest

    assert store.get_runtime_revision() == revision
    assert load_genesis(store) == manifest


def test_genesis_cannot_be_replaced(store):
    initialize_genesis(store, _manifest("domain-a"))

    with pytest.raises(ImmutableRecordConflict, match="domain genesis"):
        initialize_genesis(store, _manifest("domain-b"))


def test_sqlite_genesis_survives_reopen(tmp_path):
    path = tmp_path / "runtime.db"
    first = SQLiteStore(str(path))
    initialize_genesis(first, _manifest())
    first.close()

    reopened = SQLiteStore(str(path))
    assert load_genesis(reopened) == _manifest()
    reopened.close()


def test_sqlite_adapter_enforces_genesis_uniqueness_below_domain_service():
    store = SQLiteStore(":memory:")
    initialize_genesis(store, _manifest())
    conflicting = create_runtime_record(
        "domain.genesis", {"manifest": {"id": "untrusted-second-root"}}
    )

    with pytest.raises(ImmutableRecordConflict, match="runtime record"):
        store.append_runtime_record(conflicting)
    assert load_genesis(store) == _manifest()
    store.close()
