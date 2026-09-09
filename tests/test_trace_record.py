from aigineering.core.candidate_decision import trace_record
from aigineering.core.trace import create_entry


def test_trace_record_preserves_entry_causality_and_timestamp():
    entry = create_entry("task:one", "observed")

    record = trace_record(
        entry,
        causal_parents=("record:cause",),
        recorded_at="2026-09-08T00:00:00+00:00",
    )

    assert record.record_type == "trace.recorded"
    assert record.payload["trace"]["id"] == entry.id
    assert record.causal_parents == ("record:cause",)
    assert record.recorded_at == "2026-09-08T00:00:00+00:00"
