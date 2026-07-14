"""Versioned RuntimeRecord envelope and StorePort conformance."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    result = MemoryStore() if request.param == "memory" else SQLiteStore(":memory:")
    yield result
    if isinstance(result, SQLiteStore):
        result.close()


def test_runtime_record_payload_is_deeply_immutable():
    source = {"worker": {"capabilities": ["text", "vision"]}}
    record = create_runtime_record("worker.registered", source)
    source["worker"]["capabilities"].append("tool")

    assert record.payload["worker"]["capabilities"] == ("text", "vision")
    with pytest.raises(TypeError):
        record.payload["worker"] = {}  # type: ignore[index]


def test_runtime_record_append_scan_and_revision_are_conformant(store):
    first = create_runtime_record("contract.declared", {"contract_id": "c1"})
    second = create_runtime_record(
        "asset.committed", {"asset_id": "a1"}, causal_parents=[first.id]
    )

    assert store.append_runtime_record(first) == 1
    assert store.append_runtime_record(first) == 1
    assert store.append_runtime_record(second) == 2
    assert store.get_runtime_revision() == 2
    assert store.get_runtime_record(first.id) == first
    assert store.scan_runtime_records(after_revision=1) == [(2, second)]
    assert store.scan_runtime_records(record_type="contract.declared") == [(1, first)]


def test_runtime_record_rejects_id_reuse_and_invalid_content_id(store):
    original = create_runtime_record("asset.committed", {"asset_id": "a1"})
    store.append_runtime_record(original)

    with pytest.raises(ImmutableRecordConflict, match="runtime record"):
        store.append_runtime_record(replace(original, payload={"asset_id": "a2"}))
    with pytest.raises(ValueError, match="id mismatch"):
        store.append_runtime_record(
            RuntimeRecord(
                id="record:forged",
                record_type="asset.committed",
                payload={"asset_id": "a1"},
            )
        )


def test_sqlite_runtime_records_survive_reopen(tmp_path):
    path = tmp_path / "runtime.db"
    record = create_runtime_record("lifecycle.terminal", {"contract_id": "c1"})
    first = SQLiteStore(str(path))
    first.append_runtime_record(record)
    first.close()

    reopened = SQLiteStore(str(path))
    assert reopened.get_runtime_revision() == 1
    assert reopened.get_runtime_record(record.id) == record
    reopened.close()
