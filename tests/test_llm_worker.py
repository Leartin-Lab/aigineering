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
from aigineering.agent.worker import Worker, WorkerExecutionError
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.actions import parse_action
from aigineering.protocol.types import Asset, Contract
from conftest import candidate_runtime, hosted_worker
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
)


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
    assert payload["max_tokens"] == 2048
    assert "thinking" not in payload
    user_prompt = payload["messages"][1]["content"]
    assert "Declared outputs: report" in user_prompt
    assert "- evidence [asset_1]: observed" in user_prompt


def test_llm_worker_requires_key_for_default_transport():
    worker = LLMWorker(model="test-model", api_key="")

    with pytest.raises(ValueError, match="requires api_key"):
        worker.invoke(Contract(id="contract_1"), [])


def test_llm_worker_can_disable_provider_thinking_for_structured_actions():
    seen = {}

    def transport(url, headers, payload):
        del url, headers
        seen.update(payload)
        return {"choices": [{"message": {"content": "/exec {}"}}]}

    worker = LLMWorker(
        model="deepseek-v4-flash",
        api_key="test",
        transport=transport,
        thinking_mode="disabled",
        max_output_tokens=3072,
    )
    worker.invoke(Contract(id="task:structured"), [])

    assert seen["thinking"] == {"type": "disabled"}
    assert seen["max_tokens"] == 3072


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


@pytest.mark.parametrize(
    "kwargs",
    (
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"max_retries": -1},
        {"max_retries": True},
        {"retry_backoff": -1},
        {"retry_backoff": float("nan")},
        {"retry_backoff": float("inf")},
        {"retry_backoff": True},
        {"capacity": 1.5},
        {"capacity": True},
        {"max_output_tokens": 1.5},
        {"max_output_tokens": True},
    ),
)
def test_llmconfig_rejects_invalid_timeout_and_retries(kwargs):
    with pytest.raises(ValueError):
        LLMConfig(model="model", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"timeout": 0},
        {"max_retries": -1},
        {"retry_backoff": -1},
        {"retry_backoff": float("nan")},
        {"retry_backoff": float("inf")},
        {"retry_backoff": True},
        {"capacity": 1.5},
        {"capacity": True},
        {"max_output_tokens": 1.5},
        {"max_output_tokens": True},
    ),
)
def test_llm_worker_rejects_invalid_timeout_and_retries(kwargs):
    with pytest.raises(ValueError):
        LLMWorker(model="model", transport=_ok_transport, **kwargs)


def test_zero_retry_backoff_is_valid_for_config_and_direct_worker():
    cfg = LLMConfig(model="model", retry_backoff=0)
    worker = LLMWorker(
        model="model", config=cfg, transport=_ok_transport, retry_backoff=0
    )

    assert cfg.retry_backoff == 0
    assert worker._retry_backoff == 0.0


def test_llm_worker_exposes_routing_registration_without_prompt_injection():
    worker = LLMWorker(
        model="vision-model",
        transport=_ok_transport,
        routing_capabilities=frozenset({"vision", "strict-action"}),
        worker_pools=frozenset({"advanced"}),
        profile_id="deepseek-vision-v1",
        capacity=2,
    )

    registration = worker.registration()

    assert registration.worker_id == "llm:vision-model"
    assert registration.capabilities == ("strict-action", "vision")
    assert registration.pools == ("advanced",)
    assert registration.profile_id == "deepseek-vision-v1"
    assert registration.capacity == 2


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
    assert len(call_log) == 2  # called twice: 429, then success
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
    transport, call_log = _make_transport_with_retry(
        [ProviderError(502, "bad gateway")]
    )

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
    assert usage["model"] == "test-model"
    assert "provider" in usage


def test_usage_metadata_present_when_usage_absent():
    """No usage field in response — metadata still contains model and provider."""

    def transport(url, headers, payload):
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])
    assert candidate.metadata is not None
    metadata = dict(candidate.metadata)
    assert metadata["model"] == "test-model"
    assert "provider" in metadata


def test_truncated_completion_is_rejected_even_when_content_is_a_valid_action():
    worker = LLMWorker(
        model="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        transport=lambda *_args: {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '/exec {"outputs":{"report":"partial"}}'},
                }
            ],
            "usage": {
                "prompt_tokens": 17,
                "completion_tokens": 2048,
                "total_tokens": 2065,
            },
        },
        max_retries=0,
    )

    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(_min_contract(), [])

    assert raised.value.code == "output_truncated"
    assert dict(raised.value.usage_metadata or {}) == {
        "prompt_tokens": 17,
        "completion_tokens": 2048,
        "total_tokens": 2065,
        "model": "deepseek-flash",
        "provider": "api.deepseek.com",
        "worker_profile": "openai-compatible-v1",
    }


@pytest.mark.parametrize(
    "content", ["", "   ", None, "<think>internal reasoning only</think>"]
)
def test_empty_provider_content_is_not_replaced_by_reasoning_content(content):
    worker = LLMWorker(
        model="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        transport=lambda *_args: {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": content,
                        "reasoning_content": "/exec {}",
                    },
                }
            ],
            "usage": {"completion_tokens": 4},
        },
        max_retries=0,
    )

    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(_min_contract(), [])

    assert raised.value.code == "empty_response"
    assert dict(raised.value.usage_metadata or {})["completion_tokens"] == 4


def test_truncated_provider_failure_is_durable_with_reported_usage(tmp_path):
    store = SQLiteStore(str(tmp_path / "truncated-worker.db"))
    ingress = candidate_runtime(store)
    ingress.accept_asset(
        build_control_plane_asset(name="evidence", content="source evidence")
    )
    contract = ingress.accept_contract(
        build_control_plane_contract(
            name="truncated_report",
            inputs=("evidence",),
            outputs=("report",),
            activation="evidence",
            budget=2,
        )
    )
    worker = LLMWorker(
        model="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        transport=lambda *_args: {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '/exec {"outputs":{"report":"partial"}}'},
                }
            ],
            "usage": {"completion_tokens": 2048, "total_tokens": 2048},
        },
        max_retries=0,
    )
    host = hosted_worker(
        store,
        worker,
        genesis=ingress.genesis,
        authority_key=ingress.actor_key,
        authority_signer=ingress.signer,
    )

    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(claimed, host, store)

    failures = [
        record
        for _, record in store.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
    ]
    assert len(failures) == 1
    assert failures[0].payload["category"] == "worker_error:output_truncated"
    assert failures[0].payload["usage_metadata"]["completion_tokens"] == 2048
    assert store.get_claim(contract.id)["status"] != "active"
    assert store.get_assets_by_name("report") == []
    digest_before_rebuild = store.runtime_materialization_digest()
    store.close()

    reopened = SQLiteStore(str(tmp_path / "truncated-worker.db"))
    reopened_failures = [
        record
        for _, record in reopened.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
    ]
    assert len(reopened_failures) == 1
    assert reopened_failures[0].payload["category"] == "worker_error:output_truncated"
    assert reopened_failures[0].payload["usage_metadata"]["completion_tokens"] == 2048
    assert reopened.get_claim(contract.id)["status"] != "active"
    assert reopened.get_assets_by_name("report") == []
    assert reopened.rebuild_runtime_materializations() == digest_before_rebuild
    reopened.close()


def test_usage_metadata_partial_tokens():
    """Usage present but missing prompt/completion tokens — metadata still has model/provider."""

    def transport(url, headers, payload):
        return {
            "choices": [{"message": {"content": "report: ok"}}],
            "usage": {"total_tokens": 100},
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])
    # A valid total-only response retains total usage while missing fields stay
    # absent.
    assert candidate.metadata is not None
    metadata = dict(candidate.metadata)
    assert metadata["model"] == "test-model"
    assert "provider" in metadata
    assert metadata["total_tokens"] == 100
    assert "prompt_tokens" not in metadata
    assert "completion_tokens" not in metadata


def test_usage_metadata_drops_non_integer_partial_token_fields():
    response = {
        "choices": [{"message": {"content": '/exec {"result":"ok"}'}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3.5},
    }
    worker = LLMWorker(model="test-model", transport=lambda *_args: response)

    metadata = dict(worker.invoke(_min_contract(), []).metadata or {})

    assert metadata["prompt_tokens"] == 12
    assert "completion_tokens" not in metadata


@pytest.mark.parametrize("value", [-1, (1 << 53), True, None])
def test_usage_metadata_drops_unsafe_token_counts(value):
    response = {
        "choices": [{"message": {"content": '/exec {"result":"ok"}'}}],
        "usage": {
            "prompt_tokens": value,
            "completion_tokens": 2,
            "total_tokens": 2,
        },
    }
    worker = LLMWorker(model="test-model", transport=lambda *_args: response)

    metadata = dict(worker.invoke(_min_contract(), []).metadata or {})

    assert "prompt_tokens" not in metadata
    assert metadata["completion_tokens"] == 2
    assert metadata["total_tokens"] == 2


def test_postprocessing_error_carries_reported_usage():
    worker = LLMWorker(
        model="test-model",
        transport=lambda *_args: {
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        },
    )

    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(_min_contract(), [])

    assert dict(raised.value.usage_metadata or {})["prompt_tokens"] == 5


def test_extract_usage_valid():
    usage = _extract_usage(
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    )
    assert usage is not None
    assert dict(usage) == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }


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
    assert candidate.metadata is not None
    assert isinstance(candidate.metadata, MappingProxyType)
    assert "model" in dict(candidate.metadata)
    assert "provider" in dict(candidate.metadata)


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


def test_provider_action_serializes_structured_output_content():
    response = {
        "choices": [
            {
                "message": {
                    "content": (
                        '/exec {"outputs":{"planning_blueprint":'
                        '{"contracts":[{"name":"finish"}]}}}'
                    )
                }
            }
        ]
    }
    worker = LLMWorker(model="test-model", transport=lambda *_args: response)

    candidate = worker.invoke(_min_contract(), [])

    action = parse_action(candidate.raw_output)
    assert json.loads(action.outputs["planning_blueprint"]) == {
        "contracts": [{"name": "finish"}]
    }


def test_provider_action_removes_complete_leading_think_block():
    response = {
        "choices": [
            {
                "message": {
                    "content": (
                        "<think>private reasoning</think>\n"
                        '/exec {"outputs":{"report":"grounded"}}'
                    )
                }
            }
        ]
    }
    worker = LLMWorker(model="test-model", transport=lambda *_args: response)

    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output == '/exec {"outputs":{"report":"grounded"}}'


def test_provider_action_keeps_unclosed_think_block_for_fail_closed_parsing():
    response = {
        "choices": [
            {"message": {"content": '<think>unfinished /exec {"report":"unsafe"}'}}
        ]
    }
    worker = LLMWorker(model="test-model", transport=lambda *_args: response)

    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output.startswith("<think>")
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
    """Malformed arguments fail closed instead of changing tool semantics."""
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
    with pytest.raises(ValueError, match="not valid JSON"):
        LLMWorker._map_tool_calls_to_actions(tc)


def test_tool_mapping_error_carries_reported_usage():
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{"},
                        }
                    ]
                }
            }
        ],
        "usage": {"total_tokens": 13},
    }
    worker = LLMWorker(
        model="test-model",
        transport=lambda *_args: response,
        capabilities=frozenset({"tool_calling"}),
    )

    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(_min_contract(), [])

    assert dict(raised.value.usage_metadata or {}) == {
        "total_tokens": 13,
        "model": "test-model",
        "provider": "api.openai.com",
        "worker_profile": "openai-compatible-v1",
    }


@pytest.mark.parametrize("arguments", ['["not", "an", "object"]', 1])
def test_map_tool_calls_non_object_arguments_rejected(arguments):
    tc = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "f", "arguments": arguments},
        }
    ]
    with pytest.raises(ValueError, match="JSON object"):
        LLMWorker._map_tool_calls_to_actions(tc)


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


def test_provider_with_tool_calling_includes_only_contract_scoped_tools():
    """Provider receives only definitions named by the claimed Contract."""
    seen_payload: dict = {}

    def transport(url, headers, payload):
        seen_payload["payload"] = dict(payload)
        return {"choices": [{"message": {"content": "report: ok"}}]}

    tool_defs = [
        {"type": "function", "function": {"name": "search"}},
        {"type": "function", "function": {"name": "admin"}},
    ]
    worker = LLMWorker(
        model="test-model",
        transport=transport,
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=tool_defs,
    )
    worker.invoke(Contract(id="scoped", tool_scope=("search",)), [])

    assert "tools" in seen_payload["payload"]
    assert seen_payload["payload"]["tools"] == tool_defs[:1]


def test_provider_tool_definitions_are_omitted_when_contract_scope_is_empty():
    seen_payload: dict = {}

    def transport(url, headers, payload):
        seen_payload["payload"] = dict(payload)
        return {"choices": [{"message": {"content": "report: ok"}}]}

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=[{"type": "function", "function": {"name": "search"}}],
    )
    worker.invoke(_min_contract(), [])

    assert "tools" not in seen_payload["payload"]


def test_malformed_tool_definition_fails_before_provider_call():
    worker = LLMWorker(
        model="test-model",
        transport=lambda *_args: pytest.fail("provider must not be called"),
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=[{"type": "function"}],
    )

    with pytest.raises(ValueError, match="function"):
        worker.invoke(Contract(id="scoped", tool_scope=("search",)), [])


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


def test_timeout_triggers_retry_with_backoff():
    """socket.timeout triggers retry with increasing backoff delays."""
    import socket

    delays: list[float] = []
    call_count = [0]
    orig_sleep = time.sleep

    def fake_sleep(seconds):
        delays.append(seconds)

    def transport(url, headers, payload):
        call_count[0] += 1
        raise socket.timeout("connection timed out")

    worker = LLMWorker(
        model="test-model",
        transport=transport,
        max_retries=2,
        retry_backoff=2.0,
    )
    try:
        time.sleep = fake_sleep  # type: ignore[assignment]
        with pytest.raises(ProviderError) as exc:
            worker.invoke(_min_contract(), [])
    finally:
        time.sleep = orig_sleep  # type: ignore[assignment]

    assert exc.value.status_code == 0
    # 1 initial call + 2 retries = 3 attempts
    assert call_count[0] == 3
    # Backoff: 2^1 = 2, 2^2 = 4
    assert delays == [2.0, 4.0]


def test_429_rate_limit_retries_with_exponential_delay():
    """429 rate-limit retries with exponential delay: 2s, 4s, 8s."""
    delays: list[float] = []
    orig_sleep = time.sleep

    def fake_sleep(seconds):
        delays.append(seconds)

    def transport(url, headers, payload):
        raise ProviderError(429, "rate limit exceeded")

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


def test_5xx_transient_retries_up_to_max():
    """503 transient errors retry exactly max_retries times before giving up."""
    call_count = [0]

    def transport(url, headers, payload):
        call_count[0] += 1
        raise ProviderError(503, "service unavailable")

    worker = LLMWorker(model="test-model", transport=transport, max_retries=3)

    with pytest.raises(ProviderError) as exc:
        worker.invoke(_min_contract(), [])

    assert exc.value.status_code == 503
    # 1 initial + 3 retries = 4 total attempts
    assert call_count[0] == 4


def test_malformed_structured_output_handled():
    """LLM returns a response missing message content — graceful error, not crash."""

    def transport(url, headers, payload):
        # Response has choices but no message with content
        return {"choices": [{"index": 0, "finish_reason": "stop"}]}

    worker = LLMWorker(model="test-model", transport=transport)

    with pytest.raises(ValueError, match="missing.*(message content|choices)"):
        worker.invoke(_min_contract(), [])


def test_malformed_response_empty_choices_handled():
    """Empty choices list raises ValueError gracefully."""

    def transport(url, headers, payload):
        return {"choices": []}

    worker = LLMWorker(model="test-model", transport=transport)

    with pytest.raises(ValueError, match="missing.*(message content|choices)"):
        worker.invoke(_min_contract(), [])


def test_malformed_response_no_choices_key():
    """Missing choices key raises ValueError gracefully."""

    def transport(url, headers, payload):
        return {"data": "some unexpected structure"}

    worker = LLMWorker(model="test-model", transport=transport)

    with pytest.raises(ValueError, match="missing.*choices"):
        worker.invoke(_min_contract(), [])


def test_provider_tool_call_mapping_preserves_authority():
    """Tool calls go through /tool format, preserving name and args exactly."""

    def transport(url, headers, payload):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_xyz",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "/etc/passwd", "mode": "r"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    # Must start with /tool to go through method dispatch, not direct
    assert candidate.raw_output.startswith("/tool ")
    body = json.loads(candidate.raw_output.removeprefix("/tool ").strip())
    assert body["name"] == "read_file"
    assert body["args"] == {"path": "/etc/passwd", "mode": "r"}

    # parsed_action must have correct authority structure
    assert candidate.parsed_action is not None
    assert candidate.parsed_action["type"] == "tool"
    assert candidate.parsed_action["payload"]["name"] == "read_file"
    assert candidate.parsed_action["payload"]["args"]["path"] == "/etc/passwd"


def test_multiple_tool_calls_in_one_response():
    """Multiple tool calls compile to one parallel task-publication method."""

    def transport(url, headers, payload):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_first",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"q": "first"}',
                                },
                            },
                            {
                                "id": "call_second",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"key": "second"}',
                                },
                            },
                            {
                                "id": "call_third",
                                "type": "function",
                                "function": {
                                    "name": "fetch",
                                    "arguments": '{"url": "third"}',
                                },
                            },
                        ],
                    }
                }
            ],
        }

    worker = LLMWorker(model="test-model", transport=transport)
    candidate = worker.invoke(_min_contract(), [])

    assert candidate.raw_output.startswith("/parallel_tool ")
    body = json.loads(candidate.raw_output.removeprefix("/parallel_tool ").strip())
    assert [call["name"] for call in body["calls"]] == [
        "search",
        "lookup",
        "fetch",
    ]
    assert body["join"] == "all"
    assert candidate.parsed_action is not None
    assert candidate.parsed_action["type"] == "parallel_tool"
    payload = dict(candidate.parsed_action["payload"])
    assert tuple(call["name"] for call in payload["calls"]) == (
        "search",
        "lookup",
        "fetch",
    )


def test_no_api_key_leaked_in_error_messages():
    """Error messages must not contain the API key string under any failure path."""
    api_key = "sk-test-secret-key-aigineering-2025"

    # Scenario 1: ProviderError raised by transport
    def transport_provider_error(url, headers, payload):
        raise ProviderError(503, "backend overloaded — safe message")

    worker = LLMWorker(
        model="test-model",
        api_key=api_key,
        transport=transport_provider_error,
        max_retries=1,
    )
    with pytest.raises(ProviderError) as exc:
        worker.invoke(_min_contract(), [])
    assert api_key not in str(exc.value)
    assert api_key not in str(exc.value.__cause__ or "")

    # Scenario 2: TimeoutError raised by transport
    def transport_timeout(url, headers, payload):
        raise TimeoutError("request timed out")

    worker2 = LLMWorker(
        model="test-model",
        api_key=api_key,
        transport=transport_timeout,
        max_retries=0,
    )
    with pytest.raises(ProviderError) as exc:
        worker2.invoke(_min_contract(), [])
    assert api_key not in str(exc.value)
    assert api_key not in str(exc.value.__cause__ or "")

    # Scenario 3: OSError raised by transport
    def transport_oserror(url, headers, payload):
        raise OSError("connection reset by peer")

    worker3 = LLMWorker(
        model="test-model",
        api_key=api_key,
        transport=transport_oserror,
        max_retries=0,
    )
    with pytest.raises(ProviderError) as exc:
        worker3.invoke(_min_contract(), [])
    assert api_key not in str(exc.value)
    assert api_key not in str(exc.value.__cause__ or "")

    # Scenario 4: non-retryable error
    def transport_400(url, headers, payload):
        raise ProviderError(400, "bad request — check your payload")

    worker4 = LLMWorker(
        model="test-model",
        api_key=api_key,
        transport=transport_400,
        max_retries=1,
    )
    with pytest.raises(ProviderError) as exc:
        worker4.invoke(_min_contract(), [])
    assert api_key not in str(exc.value)
    assert api_key not in str(exc.value.__cause__ or "")


def test_json_object_mode_requests_strict_wire_and_normalizes_exec_values():
    seen = {}

    def transport(_url, _headers, payload):
        seen.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "type": "exec",
                                "outputs": {
                                    "report": "first line\n**second line**",
                                    "structured": {"items": [1, 2]},
                                },
                            }
                        )
                    }
                }
            ]
        }

    cfg = LLMConfig(model="test-model", response_format="json_object")
    worker = LLMWorker(model="ignored", config=cfg, transport=transport)
    candidate = worker.invoke(Contract(id="json", outputs=["report", "structured"]), [])
    assert seen["response_format"] == {"type": "json_object"}
    assert "Do not return slash commands" in seen["messages"][0]["content"]
    action = parse_action(candidate.raw_output)
    assert action.type == "exec"
    assert action.outputs["report"] == "first line\n**second line**"
    assert action.outputs["structured"] == '{"items":[1,2]}'


@pytest.mark.parametrize("action_type", ["plan", "replan"])
def test_json_object_mode_maps_method_actions_through_canonical_parser(action_type):
    worker = LLMWorker(
        model="test-model",
        response_format="json_object",
        transport=lambda *_args: {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"type": action_type, "payload": {"reason": "because"}}
                        )
                    }
                }
            ]
        },
    )
    candidate = worker.invoke(_min_contract(), [])
    action = parse_action(candidate.raw_output)
    assert action.type == action_type
    assert action.payload["reason"] == "because"


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        (
            "attest",
            {
                "contract_id": "task:target",
                "output_name": "report",
                "asset_id": "asset:report",
                "verdict": "accepted",
                "outputs": {
                    "receipt": "verified",
                    "details": {"evidence_ids": ["asset:a", "asset:b"]},
                },
            },
        ),
        (
            "tool",
            {
                "name": "lookup",
                "args": {
                    "query": {"text": "candidates", "filters": [{"kind": "paper"}]},
                    "limit": 3,
                },
            },
        ),
        (
            "parallel_tool",
            {
                "calls": [
                    {
                        "id": "a",
                        "name": "lookup",
                        "args": {"query": {"term": "first"}},
                    },
                    {
                        "id": "b",
                        "name": "lookup",
                        "args": {"query": {"term": "second"}},
                    },
                ],
                "join": "all",
            },
        ),
    ],
)
def test_json_object_mode_roundtrips_nested_non_exec_payloads(action_type, payload):
    wire = json.dumps({"type": action_type, "payload": payload})
    worker = LLMWorker(
        model="test-model",
        response_format="json_object",
        transport=lambda *_args: {
            "choices": [{"message": {"content": wire}}],
        },
    )

    candidate = worker.invoke(_min_contract(), [])
    action = parse_action(candidate.raw_output)

    assert action.type == action_type
    assert json.loads(candidate.raw_output.partition(" ")[2]) == payload


@pytest.mark.parametrize(
    "wire",
    [
        "[]",
        '{"type":"exec","outputs":{"report":"ok"},"extra":true}',
        '{"type":"exec","outputs":[]}',
        '{"type":"exec","outputs":{"report":"ok"}} {"type":"fail","payload":{}}',
        '{"type":"exec","outputs":{"report":"ok","report":"again"}}',
        '{"type":"unknown","payload":{}}',
        '{"type":"plan","payload":{"ratio":1e309}}',
    ],
)
def test_json_object_mode_rejects_noncanonical_wire(wire):
    worker = LLMWorker(
        model="test-model",
        response_format="json_object",
        max_retries=0,
        transport=lambda *_args: {
            "choices": [{"message": {"content": wire}}],
            "usage": {"completion_tokens": 13},
        },
    )
    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(_min_contract(), [])
    assert raised.value.code == "malformed_json_action"
    assert dict(raised.value.usage_metadata or {})["completion_tokens"] == 13


def test_json_object_mode_empty_body_keeps_empty_response_diagnostic():
    worker = LLMWorker(
        model="test-model",
        response_format="json_object",
        transport=lambda *_args: {
            "choices": [{"message": {"content": "  "}}],
            "usage": {"completion_tokens": 2},
        },
    )
    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(_min_contract(), [])
    assert raised.value.code == "empty_response"
    assert dict(raised.value.usage_metadata or {})["completion_tokens"] == 2


def test_json_object_mode_rejects_unsolicited_tool_calls():
    worker = LLMWorker(
        model="test-model",
        response_format="json_object",
        transport=lambda *_args: {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ],
            "usage": {"completion_tokens": 3},
        },
    )
    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(_min_contract(), [])
    assert raised.value.code == "json_object_tool_call_response"
    assert dict(raised.value.usage_metadata or {})["completion_tokens"] == 3


def test_json_object_mode_rejects_native_tools_before_network_call():
    called = False

    def transport(*_args):
        nonlocal called
        called = True
        return {}

    worker = LLMWorker(
        model="test-model",
        response_format="json_object",
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=[{"type": "function", "function": {"name": "lookup"}}],
        transport=transport,
    )
    contract = Contract(id="tool-contract", tool_scope=["lookup"])
    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(contract, [])
    assert raised.value.code == "json_object_with_tools_unsupported"
    assert called is False


def test_malformed_json_action_failure_is_durable_with_usage_and_rebuild(tmp_path):
    store = SQLiteStore(str(tmp_path / "malformed-json-worker.db"))
    ingress = candidate_runtime(store)
    ingress.accept_asset(
        build_control_plane_asset(name="malformed_trigger", content="trigger")
    )
    contract = ingress.accept_contract(
        build_control_plane_contract(
            name="malformed_json_report",
            inputs=(),
            outputs=("report",),
            activation="malformed_trigger",
            budget=2,
        )
    )
    worker = LLMWorker(
        model="deepseek-flash",
        response_format="json_object",
        max_retries=0,
        transport=lambda *_args: {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"type":'}}],
            "usage": {"completion_tokens": 9, "total_tokens": 9},
        },
    )
    host = hosted_worker(
        store,
        worker,
        genesis=ingress.genesis,
        authority_key=ingress.actor_key,
        authority_signer=ingress.signer,
    )
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(claimed, host, store)

    failures = [
        record
        for _, record in store.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    assert failure.payload["usage_metadata"]["completion_tokens"] == 9
    assert store.get_claim(contract.id)["status"] != "active"
    assert store.get_assets_by_name("report") == []
    digest = store.runtime_materialization_digest()
    store.close()

    reopened = SQLiteStore(str(tmp_path / "malformed-json-worker.db"))
    reopened_failures = [
        record
        for _, record in reopened.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
    ]
    assert len(reopened_failures) == 1
    assert reopened_failures[0].payload == failure.payload
    assert reopened.get_claim(contract.id)["status"] != "active"
    assert reopened.get_assets_by_name("report") == []
    assert reopened.rebuild_runtime_materializations() == digest
    reopened.close()
