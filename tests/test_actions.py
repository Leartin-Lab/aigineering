"""Tests for structured worker action parsing."""

import pytest

from aigineering.protocol.actions import (
    ActionParseError,
    action_from_dict,
    parse_action,
)


def test_parse_exec_action_outputs_wrapper():
    action = parse_action('/exec {"outputs": {"report": "ok"}}')

    assert action.type == "exec"
    assert action.outputs == {"report": "ok"}


def test_parse_exec_action_direct_output_object():
    action = parse_action('/exec {"report": "ok"}')

    assert action.type == "exec"
    assert action.outputs == {"report": "ok"}


def test_parse_method_actions_as_payload_only():
    action = parse_action('/plan {"reason": "need decomposition"}')

    assert action.type == "plan"
    assert action.outputs == {}
    assert action.payload == {"reason": "need decomposition"}


def test_parse_attestation_action_keeps_exact_binding_payload():
    action = parse_action(
        '/attest {"contract_id":"task:root","output_name":"report",'
        '"asset_id":"asset:report","verdict":"accepted",'
        '"outputs":{"receipt":"checked"}}'
    )

    assert action.type == "attest"
    assert action.payload["asset_id"] == "asset:report"
    assert action.payload["outputs"] == {"receipt": "checked"}


def test_parse_action_unwraps_one_unambiguous_quoted_tool_action():
    action = parse_action(
        '/exec {"outputs": {"/tool {\\"name\\"": '
        '"\\"openalex_search\\", \\"args\\": {\\"query\\": \\"rag\\"}}"}}'
    )

    assert action.type == "tool"
    assert action.payload == {"name": "openalex_search", "args": {"query": "rag"}}


def test_reject_unsupported_action():
    with pytest.raises(ActionParseError, match="unsupported"):
        parse_action('/mutate {"state": "done"}')


def test_action_from_dict_validates_exec_outputs():
    action = action_from_dict({"type": "exec", "outputs": {"report": "ok"}})

    assert action.type == "exec"
    assert action.outputs == {"report": "ok"}


def test_action_from_dict_rejects_non_string_output_content():
    with pytest.raises(ActionParseError, match="content"):
        action_from_dict({"type": "exec", "outputs": {"report": 123}})
