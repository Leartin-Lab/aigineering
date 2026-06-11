"""Tests for TraceStore — parametrized across MemoryTraceStore and JsonLTraceStore."""

import json
import os

import pytest

from aigineering.core.trace import (
    JsonLTraceStore,
    MemoryTraceStore,
    create_entry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(params=["memory", "jsonl"])
def trace_store(request, tmp_path):
    """Parametrized: runs each test on both Memory and JSONL stores."""
    if request.param == "memory":
        return MemoryTraceStore()
    else:
        return JsonLTraceStore(str(tmp_path / "test.jsonl"))


# ---------------------------------------------------------------------------
# Factory test (not store-specific)
# ---------------------------------------------------------------------------

def test_create_entry():
    entry = create_entry("contract_1", "activation")
    assert entry.contract_id == "contract_1"
    assert entry.event_type == "activation"
    assert entry.id.startswith("event:")
    assert entry.timestamp != ""


# ---------------------------------------------------------------------------
# Parametrized store tests (run on MemoryTraceStore AND JsonLTraceStore)
# ---------------------------------------------------------------------------

def test_trace_store_append(trace_store):
    entry = create_entry("c1", "activation", sequence=0)
    trace_store.append(entry)
    assert len(trace_store.get_all()) == 1
    assert trace_store.sequence == 1


def test_trace_store_new_entry(trace_store):
    entry = trace_store.new_entry("c1", "activation")
    assert trace_store.sequence == 1
    assert entry in trace_store.get_all()


def test_get_by_contract(trace_store):
    trace_store.new_entry("c1", "activation")
    trace_store.new_entry("c2", "activation")
    trace_store.new_entry("c1", "disclosure")
    c1_entries = trace_store.get_by_contract("c1")
    assert len(c1_entries) == 2


def test_get_by_event_type(trace_store):
    trace_store.new_entry("c1", "activation")
    trace_store.new_entry("c2", "disclosure")
    trace_store.new_entry("c3", "activation")
    assert len(trace_store.get_by_event_type("activation")) == 2
    assert len(trace_store.get_by_event_type("disclosure")) == 1


def test_get_reverse_lineage(trace_store):
    trace_store.new_entry(
        "c1", "projection",
        accepted_fragments=["asset_abc"],
    )
    trace_store.new_entry(
        "c2", "projection",
        accepted_fragments=["asset_xyz"],
    )
    lineage = trace_store.get_reverse_lineage("asset_abc")
    assert len(lineage) == 1
    assert lineage[0].contract_id == "c1"


def test_sequence_auto_increments(trace_store):
    trace_store.new_entry("c1", "activation")
    trace_store.new_entry("c1", "disclosure")
    trace_store.new_entry("c1", "projection")
    assert trace_store.sequence == 3


def test_parent_chains_do_not_cross_contracts(trace_store):
    """Parent links must stay within the same contract."""
    trace_store.new_entry("c1", "activation")
    trace_store.new_entry("c2", "activation")
    trace_store.new_entry("c1", "disclosure")
    trace_store.new_entry("c2", "disclosure")

    c1_entries = trace_store.get_by_contract("c1")
    c2_entries = trace_store.get_by_contract("c2")

    # c1's disclosure should have c1's activation as parent
    c1_disclosure = [e for e in c1_entries if e.event_type == "disclosure"][0]
    c1_activation = [e for e in c1_entries if e.event_type == "activation"][0]
    assert c1_disclosure.parent_id == c1_activation.id, (
        f"c1 disclosure parent should be c1 activation, got {c1_disclosure.parent_id}"
    )

    # c2's disclosure should have c2's activation as parent
    c2_disclosure = [e for e in c2_entries if e.event_type == "disclosure"][0]
    c2_activation = [e for e in c2_entries if e.event_type == "activation"][0]
    assert c2_disclosure.parent_id == c2_activation.id, (
        f"c2 disclosure parent should be c2 activation, got {c2_disclosure.parent_id}"
    )

    # Cross-check: c1's entries should never reference c2's entries
    c1_ids = {e.id for e in c1_entries}
    for e in c1_entries:
        if e.parent_id:
            assert e.parent_id in c1_ids, (
                f"c1 entry parent {e.parent_id} not in c1 entry set"
            )

    c2_ids = {e.id for e in c2_entries}
    for e in c2_entries:
        if e.parent_id:
            assert e.parent_id in c2_ids, (
                f"c2 entry parent {e.parent_id} not in c2 entry set"
            )


def test_new_entry_links_to_previous_entry_by_default(trace_store):
    first = trace_store.new_entry("c1", "activation")
    second = trace_store.new_entry("c1", "disclosure")
    third = trace_store.new_entry("c1", "projection")

    assert first.parent_id is None
    assert second.parent_id == first.id
    assert third.parent_id == second.id


# ---------------------------------------------------------------------------
# JSONL-specific tests (persistence, atomic flush, empty file handling)
# ---------------------------------------------------------------------------

def test_jsonl_append_and_read(tmp_path):
    """Entries written to JSONL must survive re-open and be readable."""
    path = str(tmp_path / "test_append.jsonl")
    store = JsonLTraceStore(path)
    store.new_entry("c1", "activation")
    store.new_entry("c1", "disclosure")

    # Re-open from the same file — must see both entries
    store2 = JsonLTraceStore(path)
    assert len(store2.get_all()) == 2


def test_jsonl_restores_sequence(tmp_path):
    """After re-opening, sequence counter must restore correctly."""
    path = str(tmp_path / "test_seq.jsonl")
    store = JsonLTraceStore(path)
    store.new_entry("c1", "activation")
    store.new_entry("c1", "disclosure")
    store.new_entry("c1", "projection")
    assert store.sequence == 3

    store2 = JsonLTraceStore(path)
    assert store2.sequence == 3
    # Next new_entry increments sequence to 4
    store2.new_entry("c1", "verification")
    assert store2.sequence == 4


def test_jsonl_new_entry_parent_linking(tmp_path):
    """Parent chain must survive re-open — a new entry must link to the last loaded entry of same contract."""
    path = str(tmp_path / "test_parents.jsonl")
    store = JsonLTraceStore(path)
    store.new_entry("c1", "activation")
    store.new_entry("c1", "disclosure")
    store.new_entry("c1", "projection")

    # Re-open: entries should be loaded from JSONL file
    store2 = JsonLTraceStore(path)
    loaded = store2.get_all()
    assert len(loaded) == 3

    # Parent chain must be preserved
    assert loaded[0].parent_id is None
    assert loaded[1].parent_id == loaded[0].id
    assert loaded[2].parent_id == loaded[1].id

    # New entry after re-open must link to last loaded entry of same contract
    e4 = store2.new_entry("c1", "verification")
    assert e4.parent_id == loaded[2].id


def test_jsonl_query_methods(tmp_path):
    """After re-opening, query methods must work from persisted JSONL data."""
    path = str(tmp_path / "test_query.jsonl")
    store = JsonLTraceStore(path)
    store.new_entry("c1", "activation")
    store.new_entry("c2", "activation")
    store.new_entry("c1", "disclosure",
                    accepted_fragments=["asset_abc"])
    store.new_entry("c2", "disclosure",
                    accepted_fragments=["asset_xyz"])

    # Query through reopened store to verify persistence
    store2 = JsonLTraceStore(path)
    assert len(store2.get_by_contract("c1")) == 2
    assert len(store2.get_by_contract("c2")) == 2
    assert len(store2.get_by_event_type("activation")) == 2
    assert len(store2.get_by_event_type("disclosure")) == 2
    lineage = store2.get_reverse_lineage("asset_abc")
    assert len(lineage) == 1
    assert lineage[0].contract_id == "c1"


def test_jsonl_writes_valid_line_immediately(tmp_path):
    """After writing, the JSONL file must exist immediately with valid JSON content."""
    path = str(tmp_path / "test_flush.jsonl")
    store = JsonLTraceStore(path)
    store.new_entry("c1", "activation")

    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["contract_id"] == "c1"
    assert data["event_type"] == "activation"


def test_jsonl_nonexistent_file(tmp_path):
    """A path without an existing file should yield an empty store."""
    path = str(tmp_path / "nonexistent.jsonl")
    store = JsonLTraceStore(path)
    assert store.get_all() == []
    assert store.sequence == 0


def test_jsonl_empty_file(tmp_path):
    """An existing but empty JSONL file should yield an empty store."""
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    store = JsonLTraceStore(str(path))
    assert store.get_all() == []
    assert store.sequence == 0
