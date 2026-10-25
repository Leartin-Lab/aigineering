"""Versioned RuntimeRecord envelope and StorePort conformance."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.session import (
    build_runtime_record_reference_context,
    runtime_record_contract_references,
)
from aigineering.core.store import MemoryStore
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
)
from aigineering.protocol.types import Contract
from aigineering.core.trace import create_entry, trace_record_from_entry


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
    assert store.scan_runtime_records(through_revision=1) == [(1, first)]
    assert (
        store.scan_runtime_records(
            after_revision=0, through_revision=1, record_type="asset.committed"
        )
        == []
    )
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
    record = create_runtime_record(
        "lifecycle.terminal", {"contract_id": "c1", "terminal": "complete"}
    )
    first = SQLiteStore(str(path))
    first.add_contract(Contract(id="c1", name="runtime-record-owner"))
    first.append_runtime_record(record)
    first.close()

    reopened = SQLiteStore(str(path))
    assert reopened.get_runtime_revision() == 1
    assert reopened.get_runtime_record(record.id) == record
    reopened.close()


def test_runtime_record_contract_reference_follows_committed_projection():
    declaration = create_runtime_record(
        "contract.declared",
        {"contract": {"id": "root", "parent_id": None}},
    )
    committed = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "candidate-1", "committed_record_ids": [declaration.id]},
    )
    trace = trace_record_from_entry(
        create_entry(
            "commitment",
            "candidate_committed",
            accepted_fragments=[json.dumps({"candidate_id": "candidate-1"})],
        )
    )
    index = {record.id: record for record in (declaration, committed, trace)}

    assert runtime_record_contract_references(trace, index) == {"root"}

    output_asset = create_runtime_record(
        "asset.committed",
        {
            "contract_id": "worker:mock",
            "asset": {"id": "asset-1", "created_by": "root"},
        },
    )
    qualification = create_runtime_record(
        "output.qualified",
        {"contract_id": "root", "asset_id": "asset-1"},
    )
    assert runtime_record_contract_references(output_asset) == {
        "root",
        "worker:mock",
    }
    assert runtime_record_contract_references(qualification) == {"root"}


def _commitment_trace(*candidate_ids: str):
    return trace_record_from_entry(
        create_entry(
            "commitment",
            "candidate_committed",
            accepted_fragments=[
                json.dumps({"candidate_id": candidate_id})
                for candidate_id in candidate_ids
            ],
        )
    )


def test_reference_context_keeps_all_heads_and_effective_mapping_order():
    first_member = create_runtime_record(
        "contract.declared", {"contract": {"id": "first", "parent_id": None}}
    )
    middle_member = create_runtime_record(
        "contract.declared", {"contract": {"id": "middle", "parent_id": None}}
    )
    last_member = create_runtime_record(
        "contract.declared", {"contract": {"id": "last", "parent_id": None}}
    )
    first = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "shared", "committed_record_ids": [first_member.id]},
    )
    middle = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "other", "committed_record_ids": [middle_member.id]},
    )
    last = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "shared", "committed_record_ids": [last_member.id]},
    )
    trace = _commitment_trace("other", "shared", "shared")
    index = {
        record.id: record
        for record in (
            first_member,
            first,
            middle_member,
            middle,
            last_member,
            last,
            trace,
        )
    }
    context = build_runtime_record_reference_context(index)

    assert context.records_by_id is index
    assert context.fallback_to_legacy_commitment_scan is False
    assert [
        position for position, _head in context.candidate_heads_by_id["shared"]
    ] == [
        1,
        5,
    ]
    assert runtime_record_contract_references(trace, context) == frozenset(
        {"first", "middle", "last"}
    )
    assert runtime_record_contract_references(trace, index) == frozenset(
        {"first", "middle", "last"}
    )


@pytest.mark.parametrize(
    ("head_order", "expected_error"),
    [
        (("a1", "b", "a2"), "NoneType"),
        (("a2", "b", "a1"), "mappingproxy"),
    ],
)
def test_reference_context_preserves_global_first_error_order(
    head_order, expected_error
):
    members = [
        create_runtime_record(
            "contract.declared",
            {"contract": {"id": f"valid-{index}", "parent_id": None}},
        )
        for index in range(2)
    ]
    heads = {
        "a1": create_runtime_record(
            "candidate.committed",
            {"candidate_id": "A", "committed_record_ids": [members[0].id]},
        ),
        "b": create_runtime_record(
            "candidate.committed",
            {"candidate_id": "B", "committed_record_ids": None},
        ),
        "a2": create_runtime_record(
            "candidate.committed",
            {
                "candidate_id": "A",
                "committed_record_ids": [{"unhashable": "member-id"}],
            },
        ),
    }
    trace = _commitment_trace("A", "B")
    index = {
        record.id: record
        for record in (*members, *(heads[name] for name in head_order), trace)
    }
    context = build_runtime_record_reference_context(index)

    with pytest.raises(TypeError, match=expected_error) as legacy_error:
        runtime_record_contract_references(trace, index)
    with pytest.raises(TypeError, match=expected_error) as indexed_error:
        runtime_record_contract_references(trace, context)
    assert type(indexed_error.value) is type(legacy_error.value)


def test_reference_context_lazily_falls_back_for_non_string_candidate_ids():
    malformed_head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": {"unhashable": "candidate"}, "committed_record_ids": []},
    )
    direct = create_runtime_record(
        "trace.recorded", {"trace": {"contract_id": "direct", "parent_id": None}}
    )
    empty_commitment = _commitment_trace()
    other_commitment = _commitment_trace("well-formed-but-different")
    index = {
        record.id: record
        for record in (malformed_head, direct, empty_commitment, other_commitment)
    }
    context = build_runtime_record_reference_context(index)

    assert context.fallback_to_legacy_commitment_scan is True
    assert runtime_record_contract_references(direct, context) == frozenset({"direct"})
    assert runtime_record_contract_references(direct, index) == frozenset({"direct"})
    for trace in (empty_commitment, other_commitment):
        with pytest.raises(TypeError, match="mappingproxy"):
            runtime_record_contract_references(trace, index)
        with pytest.raises(TypeError, match="mappingproxy"):
            runtime_record_contract_references(trace, context)


def test_reference_context_uses_only_effective_by_id_values():
    overwritten = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "old", "committed_record_ids": []},
    )
    survivor = replace(
        create_runtime_record(
            "candidate.committed",
            {"candidate_id": "new", "committed_record_ids": []},
        ),
        id=overwritten.id,
    )
    trace = _commitment_trace("old", "new")
    index = {record.id: record for record in (overwritten, survivor, trace)}
    context = build_runtime_record_reference_context(index)

    assert len(index) == 2
    assert context.candidate_heads_by_id.get("old") is None
    assert context.candidate_heads_by_id["new"] == ((0, survivor),)
    assert runtime_record_contract_references(trace, context) == frozenset()
    assert runtime_record_contract_references(trace, index) == frozenset()


def test_reference_context_does_not_eagerly_walk_nonmatching_members():
    malformed_member_head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": "not-selected",
            "committed_record_ids": [{"unhashable": "member-id"}],
        },
    )
    trace = _commitment_trace("selected")
    index = {record.id: record for record in (malformed_member_head, trace)}
    context = build_runtime_record_reference_context(index)

    assert runtime_record_contract_references(trace, index) == frozenset()
    assert runtime_record_contract_references(trace, context) == frozenset()


def test_reference_context_keeps_empty_mapping_and_none_fallback_equivalent():
    trace = _commitment_trace()
    empty_index = {}
    context = build_runtime_record_reference_context(empty_index)

    assert runtime_record_contract_references(trace) == frozenset()
    assert runtime_record_contract_references(trace, None) == frozenset()
    assert runtime_record_contract_references(trace, empty_index) == frozenset()
    assert runtime_record_contract_references(trace, context) == frozenset()


def test_reference_context_falls_back_for_hashable_non_string_candidate_id():
    head = create_runtime_record(
        "candidate.committed", {"candidate_id": 7, "committed_record_ids": []}
    )
    trace = _commitment_trace("candidate-string")
    index = {record.id: record for record in (head, trace)}
    context = build_runtime_record_reference_context(index)

    assert context.fallback_to_legacy_commitment_scan is True
    assert runtime_record_contract_references(trace, context) == frozenset()
    assert runtime_record_contract_references(trace, index) == frozenset()


def test_reference_context_preserves_recursive_cycle_visited_behavior():
    # This malformed pure-reader fixture is never appended to a Store.
    first = RuntimeRecord(
        id="reader-only:first",
        record_type="candidate.committed",
        payload={
            "candidate_id": "first",
            "committed_record_ids": ["reader-only:second"],
        },
    )
    second = RuntimeRecord(
        id="reader-only:second",
        record_type="candidate.committed",
        payload={
            "candidate_id": "second",
            "committed_record_ids": ["reader-only:first"],
        },
    )
    trace = _commitment_trace("first", "second")
    index = {record.id: record for record in (first, second, trace)}
    context = build_runtime_record_reference_context(index)

    assert runtime_record_contract_references(trace, index) == frozenset()
    assert runtime_record_contract_references(trace, context) == frozenset()


def test_reference_context_does_not_include_heads_after_its_source_window():
    early_contract = create_runtime_record(
        "contract.declared", {"contract": {"id": "early", "parent_id": None}}
    )
    early_head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "same", "committed_record_ids": [early_contract.id]},
    )
    trace = _commitment_trace("same")
    early_index = {record.id: record for record in (early_contract, early_head, trace)}
    later_contract = create_runtime_record(
        "contract.declared", {"contract": {"id": "later", "parent_id": None}}
    )
    later_head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "same", "committed_record_ids": [later_contract.id]},
    )
    later_index = {
        record.id: record
        for record in (*early_index.values(), later_contract, later_head)
    }
    early_context = build_runtime_record_reference_context(early_index)
    later_context = build_runtime_record_reference_context(later_index)

    assert runtime_record_contract_references(trace, early_context) == frozenset(
        {"early"}
    )
    assert runtime_record_contract_references(trace, later_context) == frozenset(
        {"early", "later"}
    )


def test_reference_context_preserves_malformed_fragment_filtering():
    contract = create_runtime_record(
        "contract.declared", {"contract": {"id": "root", "parent_id": None}}
    )
    head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "selected", "committed_record_ids": [contract.id]},
    )
    trace = create_runtime_record(
        "trace.recorded",
        {
            "trace": {
                "contract_id": "commitment",
                "accepted_fragments": [
                    None,
                    "{}",
                    "[]",
                    "not-json",
                    json.dumps({"candidate_id": 42}),
                    json.dumps({"candidate_id": "selected"}),
                ],
            }
        },
    )
    index = {record.id: record for record in (contract, head, trace)}
    context = build_runtime_record_reference_context(index)

    assert runtime_record_contract_references(trace, index) == frozenset({"root"})
    assert runtime_record_contract_references(trace, context) == frozenset({"root"})


def test_snapshot_from_path_does_not_modify_source(tmp_path):
    path = tmp_path / "readonly.db"
    source = SQLiteStore(str(path))
    source.add_contract(Contract(id="root", name="root"))
    source.close()
    before = hashlib.sha256(path.read_bytes()).digest()

    snapshot = SQLiteStore.snapshot_from_path(path)
    try:
        assert snapshot.get_contract("root") == Contract(id="root", name="root")
    finally:
        snapshot.close()

    assert hashlib.sha256(path.read_bytes()).digest() == before
