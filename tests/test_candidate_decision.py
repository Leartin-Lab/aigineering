from __future__ import annotations

import pytest

from aigineering.core.candidate_decision import (
    CommitmentDecision,
    ProjectionSnapshotConflict,
    decision_projection_fingerprint,
    require_same_projection,
)
from aigineering.core.claim_assignments import (
    AssignmentAdmission,
    AssignmentIntent,
    AssignmentMode,
    EffectFactOwner,
    SuccessorAssignmentWitness,
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


def test_decision_fingerprint_includes_runtime_record_and_trace_identity():
    assert decision_projection_fingerprint(
        _decision()
    ) != decision_projection_fingerprint(_decision(value=2))


def test_projection_comparison_rejects_changed_or_rejected_projection():
    original = _decision()
    changed = _decision(value=2)
    with pytest.raises(ProjectionSnapshotConflict):
        require_same_projection(original, changed)
    with pytest.raises(
        ProjectionSnapshotConflict,
        match="commit-time projection rejected: known test rejection",
    ):
        require_same_projection(original, _decision(accepted=False))


def test_projection_fingerprint_includes_owner_and_successor_witness_identity():
    original = _decision()
    owner = EffectFactOwner(
        "record:owner", "task.handoff", AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
    )
    intent = AssignmentIntent(
        "record:assignment",
        "contract:successor",
        "record:declaration",
        AssignmentMode.INHERIT,
    )
    witness = SuccessorAssignmentWitness(
        "contract:source",
        "record:source-assignment",
        "record:terminal",
        "contract:successor",
        "record:declaration",
        "record:assignment",
        "record:link",
        AssignmentMode.INHERIT,
    )
    owned = CommitmentDecision(
        **{
            **original.__dict__,
            "effect_fact_owners": (owner,),
            "assignment_intents": (intent,),
            "successor_assignment_witnesses": (witness,),
        }
    )
    changed_owner = CommitmentDecision(
        **{
            **owned.__dict__,
            "effect_fact_owners": (
                EffectFactOwner(
                    "record:owner", "task.handoff", AssignmentAdmission.NONE
                ),
            ),
        }
    )
    changed_witness = CommitmentDecision(
        **{
            **owned.__dict__,
            "successor_assignment_witnesses": (
                SuccessorAssignmentWitness(
                    "contract:source",
                    "record:source-assignment",
                    "record:terminal",
                    "contract:successor",
                    "record:declaration",
                    "record:assignment",
                    "record:other-link",
                    AssignmentMode.INHERIT,
                ),
            ),
        }
    )
    with pytest.raises(ProjectionSnapshotConflict):
        require_same_projection(owned, changed_owner)
    with pytest.raises(ProjectionSnapshotConflict):
        require_same_projection(owned, changed_witness)


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
