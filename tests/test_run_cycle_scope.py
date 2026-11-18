"""Cycle summaries must report proven Contract ownership only."""

import json

from aigineering.local_fleet import _cycle_trace_scope
from aigineering.core.trace import create_entry, trace_record_from_entry
from aigineering.protocol.runtime_record import create_runtime_record


def _commitment_trace(candidate_id: str):
    return trace_record_from_entry(
        create_entry(
            "commitment",
            "candidate_committed",
            accepted_fragments=[json.dumps({"candidate_id": candidate_id})],
        )
    )


def test_cycle_scope_resolves_sentinels_and_keeps_unknown_trace_unassigned():
    effect = create_runtime_record(
        "output.qualified", {"contract_id": "contract-real", "asset_id": "asset-1"}
    )
    committed = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": "candidate-known",
            "committed_record_ids": [effect.id],
        },
    )
    known_trace = _commitment_trace("candidate-known")
    unknown_trace = _commitment_trace("candidate-unknown")

    contracts, unassigned, trace_count = _cycle_trace_scope(
        [(1, effect), (2, committed), (3, known_trace), (4, unknown_trace)]
    )

    assert contracts == ["contract-real"]
    assert unassigned == [unknown_trace.payload["trace"]["id"]]
    assert trace_count == 2


def test_cycle_scope_preserves_events_even_when_no_contract_is_proven():
    untyped = _commitment_trace("not-recorded")

    contracts, unassigned, trace_count = _cycle_trace_scope([(1, untyped)])

    assert contracts == []
    assert unassigned == [untyped.payload["trace"]["id"]]
    assert trace_count == 1


def test_cycle_scope_empty_window():
    assert _cycle_trace_scope([]) == ([], [], 0)


def test_cycle_scope_does_not_join_a_commitment_outside_the_window():
    effect = create_runtime_record("output.qualified", {"contract_id": "task:earlier"})
    head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "earlier-candidate", "committed_record_ids": [effect.id]},
    )
    trace = _commitment_trace("earlier-candidate")
    assert _cycle_trace_scope([(1, effect), (2, head), (10, trace)]) == (
        ["task:earlier"],
        [],
        1,
    )
    assert _cycle_trace_scope([(10, trace)]) == (
        [],
        [trace.payload["trace"]["id"]],
        1,
    )


def test_cycle_scope_preserves_duplicate_trace_events_and_typed_references():
    trace = trace_record_from_entry(create_entry("task:known", "activation"))
    unknown = create_runtime_record("trace.recorded", {"trace": {"id": 7}})
    assert _cycle_trace_scope([(1, trace), (2, trace), (3, unknown)]) == (
        ["task:known"],
        [],
        3,
    )
