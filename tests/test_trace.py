"""Tests for TraceStore."""

from aigineering.core.trace import TraceStore, create_entry
from aigineering.protocol.types import TraceEntry


def test_create_entry():
    entry = create_entry("contract_1", "activation")
    assert entry.contract_id == "contract_1"
    assert entry.event_type == "activation"
    assert entry.id.startswith("trace_")
    assert entry.timestamp != ""


def test_trace_store_append():
    store = TraceStore()
    entry = create_entry("c1", "activation", sequence=0)
    store.append(entry)
    assert len(store.get_all()) == 1
    assert store.sequence == 1


def test_trace_store_new_entry():
    store = TraceStore()
    entry = store.new_entry("c1", "activation")
    assert store.sequence == 1
    assert entry in store.get_all()


def test_get_by_contract():
    store = TraceStore()
    store.new_entry("c1", "activation")
    store.new_entry("c2", "activation")
    store.new_entry("c1", "disclosure")
    c1_entries = store.get_by_contract("c1")
    assert len(c1_entries) == 2


def test_get_by_event_type():
    store = TraceStore()
    store.new_entry("c1", "activation")
    store.new_entry("c2", "disclosure")
    store.new_entry("c3", "activation")
    assert len(store.get_by_event_type("activation")) == 2
    assert len(store.get_by_event_type("disclosure")) == 1


def test_get_reverse_lineage():
    store = TraceStore()
    store.new_entry(
        "c1", "projection",
        accepted_fragments=["asset_abc"],
    )
    store.new_entry(
        "c2", "projection",
        accepted_fragments=["asset_xyz"],
    )
    lineage = store.get_reverse_lineage("asset_abc")
    assert len(lineage) == 1
    assert lineage[0].contract_id == "c1"


def test_sequence_auto_increments():
    store = TraceStore()
    store.new_entry("c1", "activation")
    store.new_entry("c1", "disclosure")
    store.new_entry("c1", "projection")
    assert store.sequence == 3
