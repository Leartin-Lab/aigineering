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


def test_partial_acceptance_status():
    contract = Contract(id="c1", outputs=["valid"])
    candidate = Candidate(worker_id="w", raw_output="valid: content\nundeclared: content")
    result = project_candidate(contract, candidate)
    assert result.status == ProjectionStatus.PARTIAL
    assert len(result.accepted_assets) == 1
    assert len(result.rejected_candidates) == 1


def test_all_rejected_status():
    contract = Contract(id="c1", outputs=[])
    candidate = Candidate(worker_id="w", raw_output="x: content")
    result = project_candidate(contract, candidate)
    assert result.status == ProjectionStatus.REJECTED


def test_all_accepted_status():
    contract = Contract(id="c1", outputs=["a", "b"])
    candidate = Candidate(worker_id="w", raw_output="a: one\nb: two")
    result = project_candidate(contract, candidate)
    assert result.status == ProjectionStatus.ACCEPTED


def test_multiple_rejection_categories():
    contract = Contract(id="c1", outputs=["valid"])
    candidate = Candidate(worker_id="w", raw_output="valid: content\nno colon here")
    result = project_candidate(contract, candidate)
    categories = {r.category for r in result.rejected_candidates}
    assert RejectionCategory.PARSE_ERROR in categories


def test_projection_accepts_exec_action_outputs():
    contract = Contract(id="c1", outputs=["report"])
    candidate = Candidate(
        worker_id="w",
        raw_output='/exec {"outputs": {"report": "ok"}}',
    )

    result = project_candidate(contract, candidate)

    assert result.status == ProjectionStatus.ACCEPTED
    assert result.accepted_assets[0].name == "report"
    assert result.accepted_assets[0].content == "ok"


def test_projection_rejects_non_exec_actions_as_outputs():
    contract = Contract(id="c1", outputs=["report"])
    candidate = Candidate(
        worker_id="w",
        raw_output='/plan {"reason": "need decomposition"}',
    )

    result = project_candidate(contract, candidate)

    assert result.status == ProjectionStatus.REJECTED
    assert result.accepted_assets == []
    assert result.rejected_candidates[0].name == "/plan"


def test_empty_candidate_output_is_rejected():
    contract = Contract(id="c1", outputs=["report"])
    candidate = Candidate(worker_id="w", raw_output="")

    result = project_candidate(contract, candidate)

    assert result.status == ProjectionStatus.REJECTED
    assert result.accepted_assets == []
    assert result.rejected_candidates[0].name == "(empty)"


def test_projection_rejects_invalid_parsed_action_outputs():
    contract = Contract(id="c1", outputs=["report"])
    candidate = Candidate(
        worker_id="w",
        raw_output="",
        parsed_action={"type": "exec", "outputs": {"report": 123}},
    )

    result = project_candidate(contract, candidate)

    assert result.status == ProjectionStatus.REJECTED
    assert result.accepted_assets == []
    assert result.rejected_candidates[0].name == "(action)"


def test_immutability():
    import dataclasses
    result = ProjectionResult(status=ProjectionStatus.REJECTED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = ProjectionStatus.ACCEPTED
