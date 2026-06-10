"""Tests for structured worker action parsing."""

import pytest

from aigineering.protocol.actions import ActionParseError, parse_action


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


def test_reject_unsupported_action():
    with pytest.raises(ActionParseError, match="unsupported"):
        parse_action('/mutate {"state": "done"}')
