"""Purity boundary tests — project_candidate() is a pure decision function."""

import pytest

from aigineering.core.projection import project_candidate
from aigineering.protocol.types import (
    Candidate,
    Contract,
    ProjectionResult,
    ProjectionStatus,
    RejectionCategory,
)


def test_projection_is_pure_no_store_needed():
    """project_candidate() does not take a store — it's a pure decision function."""
    contract = Contract(id="c1", name="test", outputs=["report"])
    candidate = Candidate(worker_id="w1", raw_output="report: hello")
    result = project_candidate(contract, candidate)
    assert isinstance(result, ProjectionResult)
    assert len(result.accepted_assets) == 1


def test_projection_result_all_accepted():
    """All candidate fragments match declared outputs → ACCEPTED."""
    contract = Contract(id="c1", outputs=["a", "b"])
    candidate = Candidate(worker_id="w", raw_output="a: content1\nb: content2")
    result = project_candidate(contract, candidate)
    assert result.status == ProjectionStatus.ACCEPTED
    assert len(result.accepted_assets) == 2
    assert len(result.rejected_candidates) == 0


def test_projection_result_all_rejected():
    """No declared outputs → all rejected → REJECTED."""
    contract = Contract(id="c1", outputs=[])
    candidate = Candidate(worker_id="w", raw_output="x: content")
    result = project_candidate(contract, candidate)
    assert result.status == ProjectionStatus.REJECTED
    assert len(result.accepted_assets) == 0
    assert len(result.rejected_candidates) == 1


def test_projection_result_partial():
    """Some accepted, some rejected → PARTIAL."""
    contract = Contract(id="c1", outputs=["valid"])
    candidate = Candidate(worker_id="w", raw_output="valid: content\nundeclared: content")
    result = project_candidate(contract, candidate)
    assert result.status == ProjectionStatus.PARTIAL
    assert len(result.accepted_assets) == 1
    assert len(result.rejected_candidates) == 1


def test_parse_error_has_category():
    """Parse errors get RejectionCategory.PARSE_ERROR."""
    contract = Contract(id="c1", outputs=["x"])
    candidate = Candidate(worker_id="w", raw_output="no colon here")
    result = project_candidate(contract, candidate)
    assert len(result.rejected_candidates) == 1
    assert result.rejected_candidates[0].category == RejectionCategory.PARSE_ERROR
