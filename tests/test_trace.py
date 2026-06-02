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


def test_parent_chains_do_not_cross_contracts():
    """Parent links must stay within the same contract."""
    store = TraceStore()
    store.new_entry("c1", "activation")
    store.new_entry("c2", "activation")
    store.new_entry("c1", "disclosure")
    store.new_entry("c2", "disclosure")

    c1_entries = store.get_by_contract("c1")
    c2_entries = store.get_by_contract("c2")

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


def test_new_entry_links_to_previous_entry_by_default():
    store = TraceStore()
    first = store.new_entry("c1", "activation")
    second = store.new_entry("c1", "disclosure")
    third = store.new_entry("c1", "projection")

    assert first.parent_id is None
    assert second.parent_id == first.id
    assert third.parent_id == second.id
