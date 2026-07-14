"""Regression tests for continuation terminal decisions."""

from aigineering.core.continuation_manager import _tool_observation_succeeded
from aigineering.protocol.types import Asset


def test_failed_tool_observation_does_not_authorize_continuation():
    observation = Asset(
        id="obs-failed",
        name="_tool_obs_child",
        content='{"ok": false, "error": "descriptor missing"}',
    )

    assert _tool_observation_succeeded([observation]) is False


def test_successful_tool_observation_authorizes_continuation():
    observation = Asset(
        id="obs-success",
        name="_tool_obs_child",
        content='{"ok": true, "result": "value"}',
    )

    assert _tool_observation_succeeded([observation]) is True


def test_missing_or_malformed_observation_fails_closed():
    malformed = Asset(id="obs-bad", name="_tool_obs_child", content="not-json")

    assert _tool_observation_succeeded([]) is False
    assert _tool_observation_succeeded([malformed]) is False
