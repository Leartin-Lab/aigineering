"""Behavior and query-count coverage for optional batched context Asset reads."""

import sqlite3

import pytest

from aigineering.core.asset_reads import read_assets_by_ids
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry
from aigineering.runtime import _method_context_assets_for
from aigineering.protocol.types import Asset, Contract
from conftest import candidate_runtime


def _asset(name: str, content: str, *, disclosure_view: str = "original") -> Asset:
    return sign_asset(
        Asset(
            id=f"asset:{name}",
            name=name,
            content=content,
            origin="test",
            disclosure_view=disclosure_view,
        )
    )


class _LegacyReader:
    def __init__(self, assets):
        self.assets = {asset.id: asset for asset in assets}
        self.calls: list[str] = []

    def get_asset(self, asset_id):
        self.calls.append(asset_id)
        return self.assets.get(asset_id)


class _BatchReader(_LegacyReader):
    def __init__(self, assets, extra_asset):
        super().__init__(assets)
        self.extra_asset = extra_asset
        self.batch_calls: list[tuple[str, ...]] = []

    def get_assets_by_ids(self, asset_ids):
        self.batch_calls.append(tuple(asset_ids))
        return [
            self.assets[asset_id] for asset_id in asset_ids if asset_id in self.assets
        ] + [self.extra_asset]


class _FailingBatchReader(_LegacyReader):
    def get_assets_by_ids(self, asset_ids):
        del asset_ids
        raise RuntimeError("batch read failed")


def test_optional_batch_helper_preserves_requested_order_and_filters_extras():
    first = _asset("first", "1")
    second = _asset("second", "2")
    extra = _asset("extra", "not requested")
    reader = _BatchReader((first, second), extra)

    result = read_assets_by_ids(reader, (second.id, "missing", first.id, second.id))

    assert result == [second, None, first, second]
    assert reader.batch_calls == [(second.id, "missing", first.id)]
    assert reader.calls == []


def test_legacy_reader_fallback_keeps_order_duplicates_and_missing_values():
    first = _asset("first", "1")
    second = _asset("second", "2")
    reader = _LegacyReader((first, second))

    assert read_assets_by_ids(reader, (second.id, "missing", first.id, second.id)) == [
        second,
        None,
        first,
        second,
    ]
    assert reader.calls == [second.id, "missing", first.id, second.id]


def test_optional_batch_read_errors_are_not_hidden_by_fallback():
    reader = _FailingBatchReader(())

    with pytest.raises(RuntimeError, match="batch read failed"):
        read_assets_by_ids(reader, ("asset:id",))

    assert reader.calls == []


def test_sqlite_batch_read_chunks_more_than_900_unique_ids():
    store = SQLiteStore(":memory:")
    statements: list[str] = []
    try:
        store._conn.set_trace_callback(statements.append)
        ids = tuple(f"missing:{index}" for index in range(901))
        chunk_size = min(
            900, store._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        )

        assert store.get_assets_by_ids(ids) == []

        batch_queries = [
            sql
            for sql in statements
            if sql.startswith("SELECT * FROM assets WHERE id IN (")
        ]
        assert len(batch_queries) == (len(ids) + chunk_size - 1) // chunk_size
    finally:
        store.close()


def test_sqlite_batch_read_respects_connection_variable_limit():
    store = SQLiteStore(":memory:")
    statements: list[str] = []
    try:
        store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
        store._conn.set_trace_callback(statements.append)

        assert store.get_assets_by_ids(tuple(f"missing:{i}" for i in range(5))) == []

        batch_queries = [
            sql
            for sql in statements
            if sql.startswith("SELECT * FROM assets WHERE id IN (")
        ]
        assert len(batch_queries) == 3
        assert [sql.count("'missing:") for sql in batch_queries] == [2, 2, 1]
    finally:
        store.close()


def test_disclosure_batch_query_is_exact_and_stable_after_reopen_and_rebuild(
    tmp_path,
):
    path = tmp_path / "batch-disclosure.db"
    store = SQLiteStore(str(path))
    setup = candidate_runtime(store)
    original = setup.accept_asset(_asset("context_original", "visible"))
    redacted = setup.accept_asset(
        _asset("context_redacted", "secret", disclosure_view="redacted")
    )
    unrelated = setup.accept_asset(_asset("unrelated", "not disclosed"))
    contract = Contract(
        id="task:v4:batch-disclosure",
        inputs=(),
        outputs=("result",),
        context_asset_ids=(redacted.id, "missing:context", original.id, redacted.id),
    )

    def disclose(current_store):
        sql: list[str] = []
        current_store._conn.set_trace_callback(sql.append)
        assets = compute_disclosure(contract, current_store)
        queries = [
            statement
            for statement in sql
            if statement.startswith("SELECT * FROM assets WHERE id IN (")
        ]
        return assets, queries

    before, before_queries = disclose(store)
    assert [(asset.id, asset.content) for asset in before] == [
        (redacted.id, "[redacted]"),
        (original.id, "visible"),
    ]
    assert len(before_queries) == 1
    assert unrelated.id not in {asset.id for asset in before}
    store.close()

    reopened = SQLiteStore(str(path))
    try:
        reopened.rebuild_runtime_materializations()
        after, after_queries = disclose(reopened)
        assert after == before
        assert len(after_queries) == 1
    finally:
        reopened.close()


def test_method_context_batch_read_keeps_event_order_dedup_and_redaction():
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="method-context-owner", name="owner", outputs=("result",))
    )
    first = setup.accept_asset(_asset("context_first", "first"))
    redacted = setup.accept_asset(
        _asset("context_redacted", "private", disclosure_view="redacted")
    )
    store.append_trace_entry(
        create_entry(
            contract.id,
            "method_continuation_context_prepared",
            relation_target=contract.id,
            disclosed_assets=(first.id, "missing:asset", first.id),
        )
    )
    store.append_trace_entry(
        create_entry(
            contract.id,
            "method_continuation_scheduled",
            relation_target=contract.id,
            disclosed_assets=(redacted.id, first.id),
        )
    )
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    try:
        assets = _method_context_assets_for(contract, store)
        assert [(asset.id, asset.content) for asset in assets] == [
            (first.id, "first"),
            (redacted.id, "[redacted]"),
        ]
        assert (
            sum(
                statement.startswith("SELECT * FROM assets WHERE id IN (")
                for statement in statements
            )
            == 1
        )
    finally:
        store.close()
