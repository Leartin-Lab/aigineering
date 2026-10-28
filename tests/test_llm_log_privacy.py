"""Provider exception bodies must not enter operational logs."""

from __future__ import annotations

import logging

import pytest

from aigineering.agent.llm import LLMWorker, ProviderError
from aigineering.protocol.types import Contract


_LOGGER = "aigineering.agent.llm"
_PRIVATE_MARKER = "PRIVATE-PROVIDER-BODY-401-6d98c13f"


def test_nonretryable_provider_error_logs_status_without_error_body(caplog):
    def transport(url, headers, payload):
        raise ProviderError(401, _PRIVATE_MARKER)

    worker = LLMWorker(model="fixture-model", transport=transport, max_retries=0)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(ProviderError) as raised:
            worker.invoke(Contract(id="contract:fixture"), [])

    assert "non-retryable" in caplog.text
    assert "401" in caplog.text
    assert _PRIVATE_MARKER not in caplog.text
    assert str(raised.value) == _PRIVATE_MARKER


def test_retry_logs_attempt_and_status_without_provider_error_body(caplog):
    calls = 0

    def transport(url, headers, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(503, _PRIVATE_MARKER)
        return {"choices": [{"message": {"content": "result: ok"}}]}

    worker = LLMWorker(
        model="fixture-model", transport=transport, max_retries=1, retry_backoff=0
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        candidate = worker.invoke(Contract(id="contract:fixture"), [])

    assert candidate.raw_output == "result: ok"
    assert calls == 2
    assert "retry 1/1" in caplog.text
    assert "status 503" in caplog.text
    assert _PRIVATE_MARKER not in caplog.text


def test_retry_exhaustion_logs_category_and_status_without_error_body(caplog):
    def transport(url, headers, payload):
        raise ProviderError(503, _PRIVATE_MARKER)

    worker = LLMWorker(
        model="fixture-model", transport=transport, max_retries=1, retry_backoff=0
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        with pytest.raises(ProviderError) as raised:
            worker.invoke(Contract(id="contract:fixture"), [])

    assert "retry 1/1" in caplog.text
    assert "exhausted 1 retries on status 503" in caplog.text
    assert _PRIVATE_MARKER not in caplog.text
    assert str(raised.value) == _PRIVATE_MARKER
