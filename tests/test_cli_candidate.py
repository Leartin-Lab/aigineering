"""CLI rendering never masks malformed commitment decisions."""

from __future__ import annotations

import pytest

from aigineering.cli._candidate import require_accepted
from aigineering.core.candidate_decision import CommitmentDecision
from aigineering.protocol.runtime_record import create_runtime_record


def test_require_accepted_reports_missing_rejection_record():
    decision = CommitmentDecision("candidate:bad", False, (), ())

    with pytest.raises(ValueError, match="produced no rejection record"):
        require_accepted(decision)


def test_require_accepted_uses_default_reason_for_malformed_rejection():
    rejection = create_runtime_record("candidate.rejected", {"candidate_id": "bad"})
    decision = CommitmentDecision("candidate:bad", False, (rejection,), ())

    with pytest.raises(ValueError, match="Candidate was rejected"):
        require_accepted(decision)
