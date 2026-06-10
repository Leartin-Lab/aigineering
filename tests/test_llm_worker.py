"""Tests for OpenAI-compatible LLM worker."""

import pytest

from aigineering.agent.llm import LLMWorker
from aigineering.agent.worker import Worker
from aigineering.protocol.types import Asset, Contract


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


def _ok_transport(url, headers, payload):
    return {"choices": [{"message": {"content": "report: ok"}}]}
