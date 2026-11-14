"""A bounded reader holds the connection but not SQLite's writer lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from conftest import candidate_runtime
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset


def test_read_scope_releases_on_exception_and_preserves_outer_transaction():
    with SQLiteStore() as store:
        with pytest.raises(LookupError):
            with store.read_transaction():
                assert store._conn.in_transaction
                raise LookupError("consumer failed")
        assert not store._conn.in_transaction
        with store.read_transaction():
            with pytest.raises(RuntimeError, match="own transaction"):
                with store.read_transaction():
                    pytest.fail("nested scope entered")
            assert store._conn.in_transaction
        with store.candidate_transaction():
            with pytest.raises(RuntimeError, match="own transaction"):
                with store.read_transaction():
                    pytest.fail("reader joined writer")
            assert store._candidate_transaction_active
            assert store._conn.in_transaction


def test_read_scope_holds_shared_connection_for_entire_interval():
    with SQLiteStore() as store:
        setup = candidate_runtime(store)
        entered, finished = Event(), Event()

        def writer():
            entered.set()
            asset = setup.accept_asset(
                Asset(id="ignored", name="result", content="value")
            )
            finished.set()
            return asset

        with ThreadPoolExecutor(max_workers=1) as pool:
            with store.read_transaction():
                revision = store.get_runtime_revision()
                pending = pool.submit(writer)
                assert entered.wait(5)
                assert not finished.wait(0.05)
                assert store.get_runtime_revision() == revision
            assert pending.result(timeout=5).content == "value"
        assert not store._conn.in_transaction


def test_read_scope_is_stable_while_other_connection_publishes(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    with SQLiteStore(path) as store:
        setup = candidate_runtime(store)
        with SQLiteStore(path) as other:
            publisher = candidate_runtime(
                other,
                genesis=setup.genesis,
                actor_key=setup.actor_key,
                signer=setup.signer,
            )
            with store.read_transaction():
                revision = store.get_runtime_revision()
                asset = publisher.accept_asset(
                    Asset(id="ignored", name="result", content="value")
                )
                assert store.get_runtime_revision() == revision
                assert store.get_asset(asset.id) is None
            assert store.get_asset(asset.id) == asset
