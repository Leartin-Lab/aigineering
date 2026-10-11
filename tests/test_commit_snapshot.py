from __future__ import annotations

import pytest

from aigineering.core.candidate_decision import CommitmentDecision
from aigineering.core.commit_snapshot import (
    ProjectionSnapshotConflict,
    decision_projection_fingerprint,
    make_projection_snapshot_validator,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry
from aigineering.protocol.runtime_record import create_runtime_record


def _decision(*, value=1, accepted=True):
    record = create_runtime_record("test.fact", {"value": value})
    records = (record,)
    if not accepted:
        records += (
            create_runtime_record(
                "candidate.rejected", {"reason": "known test rejection"}
            ),
        )
    return CommitmentDecision(
        candidate_id="candidate:test",
        accepted=accepted,
        runtime_records=records,
        trace_entries=(create_entry("commitment", f"event-{value}"),),
    )


def test_snapshot_fingerprint_includes_runtime_record_and_trace_identity():
    assert decision_projection_fingerprint(
        _decision()
    ) != decision_projection_fingerprint(_decision(value=2))


def test_snapshot_validator_rejects_changed_or_rejected_projection():
    original = _decision()
    changed = _decision(value=2)
    with pytest.raises(ProjectionSnapshotConflict):
        make_projection_snapshot_validator(original, lambda: changed)()
    with pytest.raises(
        ProjectionSnapshotConflict,
        match="commit-time projection rejected: known test rejection",
    ):
        make_projection_snapshot_validator(
            original, lambda: _decision(accepted=False)
        )()


def test_sqlite_runs_snapshot_validation_inside_transaction_before_writes(tmp_path):
    store = SQLiteStore(tmp_path / "snapshot.sqlite")
    observation = []

    def validate():
        observation.append(store._conn.in_transaction)
        raise ProjectionSnapshotConflict("snapshot moved")

    with pytest.raises(ProjectionSnapshotConflict, match="snapshot moved"):
        store.commit_ingress_batch(
            [], [], candidate_id="candidate:snapshot", validate_snapshot=validate
        )
    assert observation == [True]
    assert not store._conn.in_transaction
    assert store.scan_runtime_records() == []
    store.close()
