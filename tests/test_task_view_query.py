"""Snapshot binding and immutable input guarantees for aggregate task views."""

from __future__ import annotations

import pytest

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.task_view_query import (
    TaskViewQueryMismatch,
    load_task_view_query_context,
)
from test_cli_task_board import _signed_fixture


def test_query_context_preserves_real_revision_order_and_is_read_only(
    tmp_path, monkeypatch
):
    _root, _child, _services, open_store = _signed_fixture(tmp_path, monkeypatch)
    store = open_store()
    try:
        contract = store.get_all_contracts()[0]
        store.new_entry(contract.id, "query-order-first")
        store.new_entry(contract.id, "query-order-second")
        source_records = tuple(store.scan_runtime_records())
        context = load_task_view_query_context(store)
        assert context.revision == store.get_runtime_revision()
        assert context.runtime_records == source_records
        assert context.effect_projection.runtime_records == tuple(
            record for _, record in source_records
        )
        assert tuple(
            entry
            for contract in context.contracts
            for entry in context.read_store.get_by_contract(contract.id)
        ) == tuple(
            entry
            for contract in context.contracts
            for entry in store.get_by_contract(contract.id)
        )
        assert context.domain_id is not None
        assert not hasattr(context.read_store, "append_runtime_record")
        assert not hasattr(context.read_store, "commit_ingress_batch")
        with pytest.raises(AttributeError, match="immutable"):
            context.read_store._records = ()
    finally:
        store.close()


def test_query_context_rejects_other_store_changed_revision_and_mixed_projection(
    tmp_path, monkeypatch
):
    _root, _child, _services, open_store = _signed_fixture(tmp_path, monkeypatch)
    store = open_store()
    other = open_store()
    try:
        context = load_task_view_query_context(store)
        with pytest.raises(TaskViewQueryMismatch, match="different Store"):
            context.require_match(other)
        with pytest.raises(TaskViewQueryMismatch, match="projection context"):
            context.require_match(store, projection_context=EffectProjectionContext())
        store.append_runtime_record(create_runtime_record("query.test.marker", {}))
        with pytest.raises(TaskViewQueryMismatch, match="revision has changed"):
            context.require_match(store)
    finally:
        other.close()
        store.close()


def test_query_context_rejects_change_during_claim_history_read(tmp_path, monkeypatch):
    _root, _child, _services, open_store = _signed_fixture(tmp_path, monkeypatch)
    store = open_store()
    original = store.get_claim_history_contract_ids

    def append_during_claim_history_read():
        claim_ids = original()
        store.append_runtime_record(
            create_runtime_record("query.test.concurrent_marker", {})
        )
        return claim_ids

    store.get_claim_history_contract_ids = append_during_claim_history_read
    try:
        with pytest.raises(TaskViewQueryMismatch, match="changed while capturing"):
            load_task_view_query_context(store)
    finally:
        store.close()
