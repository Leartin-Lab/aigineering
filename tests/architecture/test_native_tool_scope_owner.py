"""Native response translation requires an explicit request-scoped name set."""

from inspect import Parameter, signature

from aigineering.agent.llm import LLMWorker


def test_native_tool_mapping_has_no_implicit_unrestricted_scope():
    parameter = signature(LLMWorker._map_tool_calls_to_actions).parameters[
        "allowed_names"
    ]
    assert parameter.kind is Parameter.KEYWORD_ONLY
    assert parameter.default is Parameter.empty
