"""Nested protocol values cannot change after boundary construction."""

from __future__ import annotations

import json

import pytest

from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import (
    Candidate,
    Contract,
    Session,
    ToolSpec,
    TraceEntry,
)
from aigineering.protocol.wire import (
    candidate_to_dict,
    contract_to_dict,
    session_to_dict,
    trace_entry_to_dict,
)


@pytest.mark.parametrize(
    ("value", "nested"),
    [
        (
            Candidate(
                worker_id="worker",
                raw_output="/tool",
                parsed_action={"payload": {"args": {"query": "x"}}},
            ),
            lambda item: item.parsed_action["payload"]["args"],
        ),
        (
            Contract(
                id="contract",
                sensitive_input_policy={"rules": {"origins": ["human"]}},
            ),
            lambda item: item.sensitive_input_policy["rules"],
        ),
        (
            ToolSpec(
                name="lookup", input_schema={"properties": {"q": {"type": "string"}}}
            ),
            lambda item: item.input_schema["properties"],
        ),
        (
            TraceEntry(id="trace", usage_metadata={"provider": {"tokens": [1, 2]}}),
            lambda item: item.usage_metadata["provider"],
        ),
        (
            Session(id="session", config_snapshot={"provider": {"api_key": "sealed"}}),
            lambda item: item.config_snapshot["provider"],
        ),
    ],
)
def test_nested_protocol_mappings_are_recursively_immutable(value, nested):
    with pytest.raises(TypeError):
        nested(value)["injected"] = True


def test_candidate_envelope_freezes_nested_action_and_remains_json_round_trippable():
    source = {"type": "tool", "payload": {"args": {"query": "original"}}}
    envelope = CandidateEnvelope(
        contract_id="contract",
        worker_id="worker",
        raw_output="/tool",
        parsed_action=source,
    )
    source["payload"]["args"]["query"] = "mutated"

    assert envelope.parsed_action["payload"]["args"]["query"] == "original"
    with pytest.raises(TypeError):
        envelope.parsed_action["payload"]["args"]["query"] = "mutated"
    assert CandidateEnvelope.from_json(envelope.to_json()) == envelope


def test_runtime_record_freezes_nested_payload_copy():
    source = {"candidate": {"outputs": ["report"]}}
    record = create_runtime_record("candidate.received", source)
    source["candidate"]["outputs"].append("undeclared")

    assert record.payload["candidate"]["outputs"] == ("report",)


def test_worker_package_freezes_disclosure_and_contract_before_hashing():
    contract = {"id": "contract", "policy": {"origins": ["human"]}}
    disclosed = {"id": "asset", "content": {"nested": "original"}}
    package = WorkerPackage(
        contract_id="contract",
        contract=contract,
        disclosed_assets=(disclosed,),
        method_context_assets=(),
        tool_scope=(),
        budget_remaining=1,
    )
    original_id = package.package_id
    contract["policy"]["origins"].append("worker")
    disclosed["content"]["nested"] = "mutated"

    assert package.contract["policy"]["origins"] == ("human",)
    assert package.disclosed_assets[0]["content"]["nested"] == "original"
    assert WorkerPackage.from_json(package.to_json()).package_id == original_id
    with pytest.raises(TypeError):
        package.contract["policy"]["injected"] = True


def test_wire_views_deep_thaw_protocol_values_for_json_serialization():
    candidate = Candidate(
        worker_id="worker",
        raw_output="/tool",
        parsed_action={"payload": {"args": ["x"]}},
    )
    contract = Contract(
        id="contract",
        sensitive_input_policy={"rules": {"origins": ["human"]}},
    )
    trace = TraceEntry(id="trace", usage_metadata={"provider": {"tokens": [1]}})
    session = Session(id="session", config_snapshot={"provider": {"sealed": True}})

    for view in (
        candidate_to_dict(candidate),
        contract_to_dict(contract),
        trace_entry_to_dict(trace),
        session_to_dict(session),
    ):
        json.dumps(view)
