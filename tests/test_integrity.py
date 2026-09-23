"""Local integrity reports compare immutable records to rebuildable views."""

from __future__ import annotations

import sqlite3
import hashlib

from conftest import candidate_runtime

from aigineering.core.integrity import check_store_integrity
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset


def _signed_runtime_database(path):
    store = SQLiteStore(str(path))
    publisher = candidate_runtime(store)
    publisher.accept_asset(
        Asset(id="asset:integrity-source", name="evidence", content="ok")
    )
    store.close()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_integrity_passes_for_consistent_store_and_names_unchecked_scope(
    temp_sqlite_store,
):
    publisher = candidate_runtime(temp_sqlite_store)
    publisher.accept_asset(Asset(id="asset:check", name="evidence", content="ok"))

    report = check_store_integrity(temp_sqlite_store)

    assert report["status"] == "passed"
    assert report["consistent"] is True
    assert "actor_signature_authenticity" in report["not_checked"]
    assert "runtime_record_log_completeness" in report["not_checked"]


def test_integrity_detects_changed_asset_view_without_repairing_source(tmp_path):
    path = tmp_path / "tampered.sqlite"
    store = SQLiteStore(str(path))
    publisher = candidate_runtime(store)
    asset = publisher.accept_asset(
        Asset(id="asset:tamper", name="evidence", content="ok")
    )
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE assets SET content='changed' WHERE id=?", (asset.id,)
        )
    store = SQLiteStore.snapshot_from_path(path)

    report = check_store_integrity(store)

    assert report["consistent"] is False
    assert report["materializations_match"] is False
    assert store.get_asset(asset.id).content == "changed"
    store.close()


def test_integrity_detects_edited_runtime_record_payload(tmp_path):
    path = tmp_path / "record-tamper.sqlite"
    store = SQLiteStore(str(path))
    publisher = candidate_runtime(store)
    asset = publisher.accept_asset(
        Asset(
            id="asset:tamper",
            name="evidence",
            content="PRIVATE_SENTINEL_72401",
        )
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runtime_records SET payload_json=replace(payload_json, "
            "'PRIVATE_SENTINEL_72401', 'TAMPERED_SENTINEL_72401') "
            "WHERE record_type='asset.committed' AND json_extract(payload_json, "
            "'$.asset.id')=?",
            (asset.id,),
        )
    store.close()
    store = SQLiteStore(str(path))

    report = check_store_integrity(store)

    assert report["consistent"] is False
    assert report["invalid_runtime_record_count"] == 1
    assert "PRIVATE_SENTINEL_72401" not in str(report)
    assert "TAMPERED_SENTINEL_72401" not in str(report)
    store.close()


def test_integrity_detects_deleted_trace_materialization_without_source_writes(
    tmp_path,
):
    path = tmp_path / "deleted-trace.sqlite"
    _signed_runtime_database(path)
    with sqlite3.connect(path) as connection:
        trace_id = connection.execute("SELECT id FROM trace_events LIMIT 1").fetchone()[
            0
        ]
        connection.execute("DELETE FROM trace_events WHERE id=?", (trace_id,))
    before_check = _sha256(path)

    snapshot = SQLiteStore.snapshot_from_path(path)
    report = check_store_integrity(snapshot)
    snapshot.close()

    assert report["consistent"] is False
    assert _sha256(path) == before_check


def test_integrity_detects_forged_trace_event_type_without_source_writes(tmp_path):
    path = tmp_path / "forged-trace.sqlite"
    _signed_runtime_database(path)
    with sqlite3.connect(path) as connection:
        trace_id = connection.execute("SELECT id FROM trace_events LIMIT 1").fetchone()[
            0
        ]
        connection.execute(
            "UPDATE trace_events SET event_type='forged_event' WHERE id=?", (trace_id,)
        )
    before_check = _sha256(path)

    snapshot = SQLiteStore.snapshot_from_path(path)
    report = check_store_integrity(snapshot)
    snapshot.close()

    assert report["consistent"] is False
    assert _sha256(path) == before_check


def test_integrity_handles_malformed_runtime_record_json_without_payload_leak(
    tmp_path,
):
    path = tmp_path / "malformed-record.sqlite"
    _signed_runtime_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runtime_records SET payload_json=? "
            "WHERE record_type='asset.committed'",
            ("PRIVATE_RECORD_SENTINEL {",),
        )
    before_check = _sha256(path)

    snapshot = SQLiteStore.snapshot_from_path(path)
    report = check_store_integrity(snapshot)
    snapshot.close()

    assert report["consistent"] is False
    assert report["error_type"] == "JSONDecodeError"
    assert "PRIVATE_RECORD_SENTINEL" not in str(report)
    assert "runtime_record_content_addresses" not in report["checked"]
    assert _sha256(path) == before_check
