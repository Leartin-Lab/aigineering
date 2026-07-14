"""Tests for CandidateEnvelope serialization and validation."""

import pytest

from aigineering.protocol.envelope import CandidateEnvelope


def test_candidate_envelope_round_trip_basic():
    """CandidateEnvelope survives to_json → from_json with basic fields."""
    ce = CandidateEnvelope(
        contract_id="c1",
        worker_id="mock_worker",
        raw_output="result: ok",
    )

    json_str = ce.to_json()
    restored = CandidateEnvelope.from_json(json_str)

    assert restored == ce
    assert restored.contract_id == "c1"
    assert restored.worker_id == "mock_worker"
    assert restored.claim_id == ""
    assert restored.raw_output == "result: ok"
    assert restored.parsed_action is None


def test_candidate_envelope_round_trip_with_claim_id():
    """claim_id is preserved through round-trip."""
    ce = CandidateEnvelope(
        contract_id="c1",
        worker_id="w1",
        raw_output="output",
        claim_id="claim_abc",
        claim_epoch=1,
    )

    json_str = ce.to_json()
    restored = CandidateEnvelope.from_json(json_str)

    assert restored == ce
    assert restored.claim_id == "claim_abc"


def test_candidate_envelope_round_trip_with_parsed_action():
    """parsed_action dict is preserved through round-trip."""
    ce = CandidateEnvelope(
        contract_id="c1",
        worker_id="llm:gpt-4",
        raw_output='/exec {"outputs": {"report": "ok"}}',
        parsed_action={"type": "exec", "outputs": {"report": "ok"}},
    )

    json_str = ce.to_json()
    restored = CandidateEnvelope.from_json(json_str)

    assert restored == ce
    assert restored.parsed_action == {"type": "exec", "outputs": {"report": "ok"}}


def test_candidate_envelope_constructor_rejects_empty_contract_id():
    """Empty contract_id raises ValueError in constructor."""
    with pytest.raises(ValueError, match="contract_id"):
        CandidateEnvelope(contract_id="", worker_id="w1", raw_output="x")


def test_candidate_envelope_constructor_rejects_empty_worker_id():
    """Empty worker_id raises ValueError in constructor."""
    with pytest.raises(ValueError, match="worker_id"):
        CandidateEnvelope(contract_id="c1", worker_id="", raw_output="x")


def test_candidate_envelope_constructor_rejects_empty_raw_output():
    """Empty raw_output raises ValueError in constructor."""
    with pytest.raises(ValueError, match="raw_output"):
        CandidateEnvelope(contract_id="c1", worker_id="w1", raw_output="")


def test_candidate_envelope_from_json_rejects_missing_contract_id():
    """from_json raises ValueError when contract_id is missing or empty."""
    with pytest.raises(ValueError, match="contract_id"):
        CandidateEnvelope.from_json('{"worker_id": "w1", "raw_output": "x"}')

    with pytest.raises(ValueError, match="contract_id"):
        CandidateEnvelope.from_json(
            '{"contract_id": "", "worker_id": "w1", "raw_output": "x"}'
        )


def test_candidate_envelope_from_json_rejects_missing_worker_id():
    """from_json raises ValueError when worker_id is missing or empty."""
    with pytest.raises(ValueError, match="worker_id"):
        CandidateEnvelope.from_json('{"contract_id": "c1", "raw_output": "x"}')

    with pytest.raises(ValueError, match="worker_id"):
        CandidateEnvelope.from_json(
            '{"contract_id": "c1", "worker_id": "", "raw_output": "x"}'
        )


def test_candidate_envelope_from_json_rejects_missing_raw_output():
    """from_json raises ValueError when raw_output is missing or empty."""
    with pytest.raises(ValueError, match="raw_output"):
        CandidateEnvelope.from_json('{"contract_id": "c1", "worker_id": "w1"}')

    with pytest.raises(ValueError, match="raw_output"):
        CandidateEnvelope.from_json(
            '{"contract_id": "c1", "worker_id": "w1", "raw_output": ""}'
        )


def test_candidate_envelope_multi_worker_compatibility():
    """Different worker types can produce valid CandidateEnvelopes."""
    # Simulate mock worker output
    mock_ce = CandidateEnvelope(
        contract_id="c1",
        worker_id="mock_worker",
        raw_output="result: ok",
    )

    # Simulate LLM worker output with tool action
    llm_ce = CandidateEnvelope(
        contract_id="c2",
        worker_id="llm:gpt-4.1-mini",
        raw_output='/tool {"name": "lookup", "args": {"key": "x"}}',
        parsed_action={
            "type": "tool",
            "payload": {"name": "lookup", "args": {"key": "x"}},
        },
    )

    # Simulate LLM worker output with exec action
    exec_ce = CandidateEnvelope(
        contract_id="c3",
        worker_id="llm:gpt-4",
        raw_output='/exec {"outputs": {"report": "analysis complete"}}',
        parsed_action={"type": "exec", "outputs": {"report": "analysis complete"}},
        claim_id="claim_789",
        claim_epoch=1,
    )

    # All envelopes serialize and deserialize correctly
    for ce in (mock_ce, llm_ce, exec_ce):
        json_str = ce.to_json()
        restored = CandidateEnvelope.from_json(json_str)
        assert restored == ce
        assert isinstance(restored.contract_id, str) and restored.contract_id
        assert isinstance(restored.worker_id, str) and restored.worker_id
        assert isinstance(restored.raw_output, str) and restored.raw_output


def test_candidate_envelope_default_claim_id_is_empty_string():
    """claim_id defaults to empty string when not provided."""
    ce = CandidateEnvelope(
        contract_id="c1",
        worker_id="w1",
        raw_output="output",
    )
    assert ce.claim_id == ""

    json_str = ce.to_json()
    restored = CandidateEnvelope.from_json(json_str)
    assert restored.claim_id == ""
