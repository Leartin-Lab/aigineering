"""Backup-first diagnosis retains evidence without repairing the source."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from conftest import candidate_runtime

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.diagnostics import main, verify_reconstruction
from aigineering.protocol.types import Asset


@pytest.fixture
def live_store(tmp_path):
    store = SQLiteStore(str(tmp_path / "live.sqlite"))
    publisher = candidate_runtime(store)
    publisher.accept_asset(
        Asset(
            id="asset:private-evidence", name="evidence", content="private-test-content"
        ),
        source="test",
    )
    publisher.accept_contract(
        build_control_plane_contract(name="report", outputs=("report",))
    )
    try:
        yield store
    finally:
        store.close()


def test_backup_includes_committed_wal_and_preserves_source(live_store, tmp_path):
    digest = live_store.runtime_materialization_digest()
    revision = live_store.get_runtime_revision()
    output = tmp_path / "evidence"

    report = verify_reconstruction(tmp_path / "live.sqlite", output)

    assert report["status"] == "passed"
    assert report["before_digest"] == report["after_digest"] == digest
    assert report["records_unchanged"] is True
    assert report["before_tables"]["runtime_records"]["rows"] == revision
    assert live_store.runtime_materialization_digest() == digest
    assert live_store.get_runtime_revision() == revision
    serialized = (output / "manifest.json").read_text()
    assert json.loads(serialized) == report
    assert "private-test-content" not in serialized
    for name in ("before.sqlite", "rebuilt.sqlite", "manifest.json"):
        assert (output / name).is_file()
        if os.name != "nt":
            assert (output / name).stat().st_mode & 0o077 == 0
    if os.name != "nt":
        assert output.stat().st_mode & 0o077 == 0


def test_mismatch_retains_corrupt_view_and_rebuilt_copy(live_store, tmp_path):
    path = tmp_path / "live.sqlite"
    # Deliberate disk-corruption injection after signed publication. This is
    # the failure being diagnosed, not an alternate way to create test facts.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE contracts SET description='corrupt-view'")
    corrupted_digest = live_store.runtime_materialization_digest()
    output = tmp_path / "mismatch"

    report = verify_reconstruction(path, output)

    assert report["status"] == "mismatch"
    assert report["records_unchanged"] is True
    assert "contracts" in report["changed_tables"]
    assert report["before_digest"] != report["after_digest"]
    assert live_store.runtime_materialization_digest() == corrupted_digest
    with sqlite3.connect(output / "before.sqlite") as connection:
        assert (
            connection.execute("SELECT description FROM contracts").fetchone()[0]
            == "corrupt-view"
        )
    with sqlite3.connect(output / "rebuilt.sqlite") as connection:
        assert (
            connection.execute("SELECT description FROM contracts").fetchone()[0]
            != "corrupt-view"
        )


def test_rebuild_exception_retains_both_copies_without_exporting_error_text(
    live_store, tmp_path, monkeypatch
):
    def fail(_store):
        raise ValueError("private-test-content")

    monkeypatch.setattr(SQLiteStore, "rebuild_runtime_materializations", fail)
    output = tmp_path / "error"
    report = verify_reconstruction(tmp_path / "live.sqlite", output)

    assert report["status"] == "error"
    assert report["error_type"] == "ValueError"
    assert "private-test-content" not in (output / "manifest.json").read_text()
    assert (output / "before.sqlite").exists()
    assert (output / "rebuilt.sqlite").exists()


def test_evidence_directory_is_never_overwritten(live_store, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "manifest.json"
    sentinel.write_text("retained")
    with pytest.raises(FileExistsError):
        verify_reconstruction(tmp_path / "live.sqlite", output)
    assert sentinel.read_text() == "retained"


def test_cli_nonzero_for_mismatch_and_missing_database(live_store, tmp_path, capsys):
    path = tmp_path / "live.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE contracts SET description='corrupt-view'")
    assert main([str(path), "--output-dir", str(tmp_path / "cli-evidence")]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "mismatch"
    assert (
        main(
            [
                str(tmp_path / "missing.sqlite"),
                "--output-dir",
                str(tmp_path / "missing"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "missing.sqlite").exists()


def test_old_schema_is_retained_without_migration(live_store, tmp_path):
    path = tmp_path / "live.sqlite"
    # Simulate an incompatible database version; diagnostic copies must not
    # silently migrate a version the current evidence protocol cannot compare.
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version VALUES (1, 'test')")
    report = verify_reconstruction(path, tmp_path / "old-schema")
    assert report["status"] == "error"
    assert report["error_type"] == "UnsupportedDiagnosticSchema"
    assert report["schema_version"] == 1
    assert (tmp_path / "old-schema" / "before.sqlite").exists()
