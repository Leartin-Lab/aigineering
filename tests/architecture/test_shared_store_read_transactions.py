"""Readers, snapshot backup and Candidate writers cannot share a transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from conftest import candidate_runtime
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore, _SerializedConnection
from aigineering.protocol.types import Asset


def test_reader_holds_connection_through_cursor_fetch(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "read-write.sqlite") as store:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(name="reader", outputs=("result",), budget=1)
        )
        fetched, release, writer_entered, writer_finished = (
            Event(),
            Event(),
            Event(),
            Event(),
        )
        execute = _SerializedConnection.execute

        class DelayedCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                fetched.set()
                assert release.wait(5)
                return self.cursor.fetchone()

        def delayed(connection, sql, *args, **kwargs):
            cursor = execute(connection, sql, *args, **kwargs)
            if sql == "SELECT * FROM contracts WHERE id = ?" and not release.is_set():
                return DelayedCursor(cursor)
            return cursor

        monkeypatch.setattr(_SerializedConnection, "execute", delayed)

        def publish():
            writer_entered.set()
            result = setup.accept_asset(
                Asset(id="ignored", name="result", content="actual")
            )
            writer_finished.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(store.get_contract, task.id)
            assert fetched.wait(5)
            writer = pool.submit(publish)
            assert writer_entered.wait(5)
            try:
                assert not writer_finished.wait(0.05)
                assert not writer.done()
                assert not store._conn.in_transaction
            finally:
                release.set()
            assert reader.result(timeout=5) == task
            assert writer.result(timeout=5).name == "result"
        assert not store._conn.in_transaction


def test_snapshot_waits_for_whole_candidate_transaction(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "snapshot-write.sqlite") as store:
        setup = candidate_runtime(store)
        inserted, release, snapshot_started, snapshot_finished = (
            Event(),
            Event(),
            Event(),
            Event(),
        )
        insert = store._insert_ingress_assets

        def delayed(assets):
            insert(assets)
            inserted.set()
            assert release.wait(5)

        monkeypatch.setattr(store, "_insert_ingress_assets", delayed)

        def snapshot():
            snapshot_started.set()
            result = store.snapshot()
            snapshot_finished.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(
                setup.accept_asset, Asset(id="ignored", name="result", content="actual")
            )
            assert inserted.wait(5)
            reader = pool.submit(snapshot)
            assert snapshot_started.wait(5)
            try:
                assert not snapshot_finished.wait(0.05)
                assert not reader.done()
            finally:
                release.set()
            asset = writer.result(timeout=5)
            with reader.result(timeout=5) as copied:
                assert copied.get_asset(asset.id) == asset
                assert copied.get_runtime_revision() == store.get_runtime_revision()
                assert copied.scan_runtime_records(record_type="candidate.committed")
        assert not store._conn.in_transaction
