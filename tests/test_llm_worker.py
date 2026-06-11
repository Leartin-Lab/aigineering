"""Tests for OpenAI-compatible LLM worker."""

from __future__ import annotations

import json
import time
from types import MappingProxyType

import pytest

from aigineering.agent.llm import (
    LLMConfig,
    LLMWorker,
    ProviderError,
    SUPPORTED_CAPABILITIES,
    _extract_tool_calls,
    _extract_usage,
)
from aigineering.agent.worker import Worker
from aigineering.protocol.types import Asset, Contract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_transport(url, headers, payload):
    return {"choices": [{"message": {"content": "report: ok"}}]}


def _min_contract():
    return Contract(id="contract_1")


def _make_transport_with_retry(errors_before_success):
    """
    Return (transport, call_log) where:
      - transport raises ProviderError error_count times, then returns success
      - call_log is mutated in-place, recording each call's (url, error_raised)
    """
    call_log: list[dict] = []
    state = {"remaining": list(errors_before_success)}

    def transport(url, headers, payload):
        call_log.append({"url": url})
        if state["remaining"]:
            err = state["remaining"].pop(0)
            call_log[-1]["error"] = err
            raise err
        return {"choices": [{"message": {"content": "report: ok"}}]}

    return transport, call_log


# ---------------------------------------------------------------------------
# Existing tests (kept as-is)
# ---------------------------------------------------------------------------

def test_llm_worker_satisfies_worker_protocol():
    worker = LLMWorker(model="test-model", transport=_ok_transport)

    assert isinstance(worker, Worker)


def test_llm_worker_returns_candidate_from_chat_completion():
    seen: dict[str, object] = {}

    def transport(url, headers, payload):
        seen["url"] = url
        seen["headers"] = headers
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(
        model="test-model",
        api_key="test-key",
        base_url="https://llm.example/v1",
        transport=transport,
    )
    contract = Contract(
        id="contract_1",
        name="write_report",
        description="Write a concise report.",
        inputs=["evidence"],
        outputs=["report"],
    )
    candidate = worker.invoke(
        contract,
        [Asset(id="asset_1", name="evidence", content="observed")],
    )

    assert candidate.worker_id == "llm:test-model"
    assert candidate.raw_output == "report: ok"
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer test-key"

    payload = seen["payload"]
    assert payload["model"] == "test-model"
    user_prompt = payload["messages"][1]["content"]
    assert "Declared outputs: report" in user_prompt
    assert "- evidence: observed" in user_prompt


def test_llm_worker_requires_key_for_default_transport():
    worker = LLMWorker(model="test-model", api_key="")

    with pytest.raises(ValueError, match="requires api_key"):
        worker.invoke(Contract(id="contract_1"), [])


# ---------------------------------------------------------------------------
# LLMConfig tests
# ---------------------------------------------------------------------------

def test_llmconfig_defaults():
    cfg = LLMConfig(model="gpt-4.1")
    assert cfg.model == "gpt-4.1"
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == ""
    assert cfg.timeout == 60.0
    assert cfg.max_retries == 3
    assert cfg.retry_backoff == 2.0


def test_llm_worker_from_config():
    cfg = LLMConfig(model="cfg-model", timeout=30.0, max_retries=5, retry_backoff=3.0)
    worker = LLMWorker(model="ignored", config=cfg, transport=_ok_transport)

    assert worker.model == "cfg-model"
    assert worker._timeout == 30.0
    assert worker._max_retries == 5
    assert worker._retry_backoff == 3.0


def test_llm_worker_config_with_api_key():
    cfg = LLMConfig(model="cfg-model", api_key="from-config")
    worker = LLMWorker(
        model="ignored",
        api_key="from-kwarg",
        config=cfg,
    )
    assert worker.api_key == "from-config"  # config takes precedence


# ---------------------------------------------------------------------------
# ProviderError tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status_code, expected_retryable",
    [
        (429, True),
        (502, True),
        (503, True),
        (504, True),
        (500, True),
        (501, True),
        (505, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (409, False),
    ],
)
def test_provider_error_retryability(status_code, expected_retryable):
    err = ProviderError(status_code, "test")
    assert err.is_retryable == expected_retryable
    assert err.status_code == status_code


# ---------------------------------------------------------------------------
# Retry logic tests
# ---------------------------------------------------------------------------

def test_retry_on_429_succeeds():
    """Simulate one 429 then success — verify retry happens and succeeds."""
    transport, call_log = _make_transport_with_retry([ProviderError(429, "rate limit")])

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        max_retries=3,
    )
    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output == "report: ok"
    assert len(call_log) == 2           # called twice: 429, then success
    assert call_log[0].get("error") is not None
    assert "error" not in call_log[1]


def test_no_retry_on_400():
    """Simulate a 400 — verify immediate error with NO retry."""
    def transport(url, headers, payload):
        raise ProviderError(400, "bad request")

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        max_retries=3,
    )
    with pytest.raises(ProviderError) as exc:
        worker.invoke(_min_contract(), [])
    assert exc.value.status_code == 400
    assert not exc.value.is_retryable


def test_timeout_retry():
    """Simulate one timeout, then success — verify retry happens."""
    call_count = [0]

    def transport(url, headers, payload):
        call_count[0] += 1
        if call_count[0] == 1:
            raise TimeoutError("timed out")
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(model="test-model", transport=transport, max_retries=2)
    candidate = worker.invoke(_min_contract(), [])
    assert candidate.raw_output == "report: ok"
    assert call_count[0] == 2


def test_max_retries_exceeded():
    """All retries fail — verify ProviderError is raised after exhausting retries."""
    def transport(url, headers, payload):
        raise ProviderError(503, "service unavailable")

    worker = LLMWorker(model="test-model", transport=transport, max_retries=2)
    with pytest.raises(ProviderError) as exc:
        worker.invoke(_min_contract(), [])
    assert exc.value.status_code == 503


def test_backoff_increases_with_attempts():
    """Verify backoff delay grows: 2s, 4s, 8s for attempts 1,2,3."""
    delays: list[float] = []
    orig_sleep = time.sleep

    def fake_sleep(seconds):
        delays.append(seconds)

    def transport(url, headers, payload):
        raise ProviderError(503, "unavailable")

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        max_retries=3,
        retry_backoff=2.0,
    )
    try:
        time.sleep = fake_sleep  # type: ignore[assignment]
        with pytest.raises(ProviderError):
            worker.invoke(_min_contract(), [])
    finally:
        time.sleep = orig_sleep  # type: ignore[assignment]

    assert delays == [2.0, 4.0, 8.0]


def test_retry_on_5xx_retries():
    """Simulate one 502 then success — retry should happen."""
    transport, call_log = _make_transport_with_retry([ProviderError(502, "bad gateway")])

    worker = LLMWorker(model="test-model", transport=transport, max_retries=3)
    candidate = worker.invoke(_min_contract(), [])
    assert candidate.raw_output == "report: ok"
    assert len(call_log) == 2


# ---------------------------------------------------------------------------
# Usage metadata tests
# ---------------------------------------------------------------------------

def test_usage_metadata_captured():
    """Mock response with usage — verify metadata is tracked in Candidate."""
    def transport(url, headers, payload):
        return {
            "choices": [{"message": {"content": "report: ok"}}],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 7,
                "total_tokens": 49,
            },
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    assert candidate.metadata is not None
    usage = dict(candidate.metadata)
    assert usage["prompt_tokens"] == 42
    assert usage["completion_tokens"] == 7
    assert usage["total_tokens"] == 49


def test_usage_metadata_none_when_absent():
    """No usage field in response — metadata should be None."""
    def transport(url, headers, payload):
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])
    assert candidate.metadata is None


def test_usage_metadata_partial_tokens():
    """Usage present but missing prompt/completion tokens — metadata should be None."""
    def transport(url, headers, payload):
        return {
            "choices": [{"message": {"content": "report: ok"}}],
            "usage": {"total_tokens": 100},
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])
    assert candidate.metadata is None


def test_extract_usage_valid():
    usage = _extract_usage({"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
    assert usage is not None
    assert dict(usage) == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def test_extract_usage_missing():
    assert _extract_usage({}) is None
    assert _extract_usage({"usage": {}}) is None
    assert _extract_usage({"usage": "not a dict"}) is None


# ---------------------------------------------------------------------------
# Metadata immutability test
# ---------------------------------------------------------------------------

def test_candidate_metadata_is_mappingproxy():
    """Metadata stored on Candidate must be an immutable MappingProxyType."""
    def transport(url, headers, payload):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])
    assert candidate.metadata is None or isinstance(candidate.metadata, MappingProxyType)


# ---------------------------------------------------------------------------
# Provider-native tool calling tests (v0.4.4)
# ---------------------------------------------------------------------------

_SAMPLE_TOOL_CALLS = [
    {
        "id": "call_abc123",
        "type": "function",
        "function": {
            "name": "search",
            "arguments": '{"q": "hello"}',
        },
    }
]


def test_tool_calls_mapped_to_method_requests():
    """Simulate tool_calls from provider → /tool method request format."""
    def transport(url, headers, payload):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": _SAMPLE_TOOL_CALLS,
                    }
                }
            ],
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output.startswith("/tool ")
    assert "search" in candidate.raw_output
    assert candidate.parsed_action is not None
    assert candidate.parsed_action["type"] == "tool"
    assert candidate.parsed_action["payload"]["name"] == "search"
    assert candidate.parsed_action["payload"]["args"] == {"q": "hello"}


def test_tool_calls_never_execute_directly():
    """Tool calls go through method dispatch, never executed by provider."""
    def transport(url, headers, payload):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": _SAMPLE_TOOL_CALLS,
                    }
                }
            ],
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output.startswith("/tool ")
    assert "search" in candidate.raw_output
    assert isinstance(candidate.parsed_action, MappingProxyType)

    raw_action = json.loads(candidate.raw_output.removeprefix("/tool ").strip())
    assert raw_action["name"] == "search"


def test_tool_call_format_valid():
    """Output matches expected /tool action format."""
    def transport(url, headers, payload):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": _SAMPLE_TOOL_CALLS,
                    }
                }
            ],
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    parsed = candidate.parsed_action
    assert parsed["type"] == "tool"
    assert isinstance(parsed, MappingProxyType)
    assert parsed["payload"]["name"] == "search"
    assert parsed["payload"]["args"] == {"q": "hello"}

    raw_output = candidate.raw_output
    assert raw_output.startswith("/tool ")
    body = json.loads(raw_output.removeprefix("/tool ").strip())
    assert body == {"name": "search", "args": {"q": "hello"}}


def test_content_response_unchanged_without_tool_calls():
    """Text-only responses (no tool_calls) still work as before."""
    def transport(url, headers, payload):
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output == "report: ok"
    assert candidate.parsed_action is None


def test_tool_calls_take_priority_over_content():
    """When both content and tool_calls present, tool_calls win."""
    def transport(url, headers, payload):
        return {
            "choices": [
                {
                    "message": {
                        "content": "I will use a tool for that.",
                        "tool_calls": _SAMPLE_TOOL_CALLS,
                    }
                }
            ],
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output.startswith("/tool ")
    assert candidate.parsed_action is not None
    assert candidate.parsed_action["type"] == "tool"


def test_map_tool_calls_empty_raises():
    """Empty tool_calls list raises ValueError."""
    with pytest.raises(ValueError, match="must not be empty"):
        LLMWorker._map_tool_calls_to_actions([])


def test_map_tool_calls_missing_function_raises():
    """Tool call without 'function' key raises ValueError."""
    with pytest.raises(ValueError, match="missing 'function'"):
        LLMWorker._map_tool_calls_to_actions([{"id": "x", "type": "function"}])


def test_map_tool_calls_missing_name_raises():
    """Tool call without valid name raises ValueError."""
    bad = [{"id": "x", "type": "function", "function": {"arguments": "{}"}}]
    with pytest.raises(ValueError, match="missing valid 'name'"):
        LLMWorker._map_tool_calls_to_actions(bad)


def test_map_tool_calls_non_string_arguments():
    """Arguments field that is already a dict should work."""
    tc = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "lookup",
                "arguments": {"key": "val"},
            },
        }
    ]
    raw_output, parsed_action = LLMWorker._map_tool_calls_to_actions(tc)
    assert parsed_action["payload"]["args"] == {"key": "val"}
    assert json.loads(raw_output.removeprefix("/tool ").strip()) == {
        "name": "lookup",
        "args": {"key": "val"},
    }


def test_map_tool_calls_invalid_json_arguments():
    """Malformed JSON in arguments defaults to empty dict."""
    tc = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "f",
                "arguments": "not json",
            },
        }
    ]
    raw_output, parsed_action = LLMWorker._map_tool_calls_to_actions(tc)
    assert parsed_action["payload"]["args"] == {}


def test_provider_without_tool_calling_capability():
    """Provider without 'tool_calling' capability does not include tools in payload."""
    seen_payload: dict = {}

    def transport(url, headers, payload):
        seen_payload["payload"] = dict(payload)
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        capabilities=frozenset(),
        tool_definitions=[{"type": "function", "function": {"name": "search"}}],
    )
    worker.invoke(_min_contract(), [])

    assert "tools" not in seen_payload["payload"]


def test_provider_with_tool_calling_includes_tools():
    """Provider with 'tool_calling' capability includes tool definitions in payload."""
    seen_payload: dict = {}

    def transport(url, headers, payload):
        seen_payload["payload"] = dict(payload)
        return {"choices": [{"message": {"content": "report: ok"}}]}

    tool_defs = [{"type": "function", "function": {"name": "search"}}]
    worker = LLMWorker(
        model="test-model",
        transport=transport,
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=tool_defs,
    )
    worker.invoke(_min_contract(), [])

    assert "tools" in seen_payload["payload"]
    assert seen_payload["payload"]["tools"] == tool_defs


def test_tool_calling_empty_definitions_skips_tools():
    """When tool_definitions is empty, tools key is not included in payload."""
    seen_payload: dict = {}

    def transport(url, headers, payload):
        seen_payload["payload"] = dict(payload)
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=[],
    )
    worker.invoke(_min_contract(), [])

    assert "tools" not in seen_payload["payload"]


def test_tool_calling_none_definitions_skips_tools():
    """When tool_definitions is None, tools key is not included in payload."""
    seen_payload: dict = {}

    def transport(url, headers, payload):
        seen_payload["payload"] = dict(payload)
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=None,
    )
    worker.invoke(_min_contract(), [])

    assert "tools" not in seen_payload["payload"]


def test_llmconfig_capabilities_default():
    """LLMConfig capabilities defaults to empty frozenset."""
    cfg = LLMConfig(model="gpt-4")
    assert cfg.capabilities == frozenset()
    assert cfg.tool_definitions is None


def test_llmconfig_capabilities_custom():
    """LLMConfig accepts custom capabilities and tool_definitions."""
    tool_defs = [{"type": "function", "function": {"name": "search"}}]
    cfg = LLMConfig(
        model="gpt-4",
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=tool_defs,
    )
    assert cfg.capabilities == frozenset({"tool_calling"})
    assert cfg.tool_definitions == tool_defs


def test_worker_from_config_with_tool_calling():
    """Worker inherits capabilities and tool_definitions from config."""
    tool_defs = [{"type": "function", "function": {"name": "search"}}]
    cfg = LLMConfig(
        model="cfg-model",
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=tool_defs,
    )
    worker = LLMWorker(model="ignored", config=cfg, transport=_ok_transport)
    assert worker._capabilities == frozenset({"tool_calling"})
    assert worker._tool_definitions == tool_defs


def test_supported_capabilities_const():
    """SUPPORTED_CAPABILITIES defines known provider features."""
    assert isinstance(SUPPORTED_CAPABILITIES, frozenset)
    assert "tool_calling" in SUPPORTED_CAPABILITIES
    assert "json_schema" in SUPPORTED_CAPABILITIES


def test_extract_tool_calls_none_when_absent():
    """Message without tool_calls returns None."""
    response = {"choices": [{"message": {"content": "hello"}}]}
    assert _extract_tool_calls(response) is None


def test_extract_tool_calls_present():
    """Message with tool_calls returns the list."""
    response = {"choices": [{"message": {"tool_calls": _SAMPLE_TOOL_CALLS}}]}
    result = _extract_tool_calls(response)
    assert result == _SAMPLE_TOOL_CALLS


def test_extract_tool_calls_empty_list_returns_none():
    """Empty tool_calls list returns None."""
    response = {"choices": [{"message": {"tool_calls": []}}]}
    assert _extract_tool_calls(response) is None


def test_tool_calls_go_through_method_dispatch_e2e():
    """Full e2e: provider returns tool_calls, engine dispatches as /tool method.

    First invocation returns a provider-native tool_calls, which is mapped
    to a ``/tool`` method request.  After the tool executes and the parent
    resumes, the second invocation produces the declared output.
    """
    from aigineering.core.engine import Engine
    from aigineering.core.store import MemoryStore
    from aigineering.core.tools import ToolRegistry
    from aigineering.core.trace import TraceStore
    from aigineering.protocol.types import ToolSpec

    responses = [
        # First call: provider-native tool_calls (mapped to /tool)
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"key": "x"}',
                                },
                            }
                        ],
                    }
                }
            ],
        },
        # Second call: after tool execution, produce the declared output
        {
            "choices": [
                {
                    "message": {
                        "content": '/exec {"outputs": {"result": "final answer"}}',
                    }
                }
            ],
        },
    ]

    def transport(url, headers, payload):
        return responses.pop(0)

    worker = LLMWorker(model="test-model", transport=transport)
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_native_tool",
        name="native_tool_test",
        description="Test provider-native tool calling.",
        outputs=["result"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
    )

    engine = Engine(store, worker, trace_store, tools=tools)
    engine.add_contract(contract)
    engine.run()

    result_assets = store.get_assets_by_name("result")
    assert len(result_assets) == 1
    assert result_assets[0].content == "final answer"

    obs_assets = store.get_assets_by_name("_tool_obs_contract_native_tool")
    assert len(obs_assets) == 1
    assert "value:x" in obs_assets[0].content
