"""Bounded decoding for provider HTTP responses."""

import urllib.error

import pytest

from aigineering.agent.llm import (
    MAX_PROVIDER_RESPONSE_BYTES,
    LLMWorker,
    ProviderError,
    _post_json,
)
from aigineering.agent.worker import WorkerExecutionError
from aigineering.protocol.types import Contract


class _Response:
    def __init__(self, body):
        self.body = body
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


def _post(monkeypatch, body):
    response = _Response(body)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    return response


def test_response_read_is_capped_at_limit_plus_one(monkeypatch):
    body = b"{}" + b" " * (MAX_PROVIDER_RESPONSE_BYTES - 2)
    response = _post(monkeypatch, body)

    assert _post_json("https://llm.example/v1/chat/completions", {}, {}, 1) == {}
    assert response.read_sizes == [MAX_PROVIDER_RESPONSE_BYTES + 1]


def test_response_over_limit_is_stable_and_not_retried(monkeypatch):
    response = _post(monkeypatch, b"{}" + b" " * (MAX_PROVIDER_RESPONSE_BYTES - 1))
    worker = LLMWorker(model="test-model", api_key="key", max_retries=3)

    with pytest.raises(WorkerExecutionError) as exc_info:
        worker.invoke(Contract(id="contract_1"), [])

    assert exc_info.value.code == "response_too_large"
    assert response.read_sizes == [MAX_PROVIDER_RESPONSE_BYTES + 1]


def test_invalid_utf8_has_stable_worker_error(monkeypatch):
    _post(monkeypatch, b"\xff")

    with pytest.raises(WorkerExecutionError) as exc_info:
        _post_json("https://llm.example/v1/chat/completions", {}, {}, 1)

    assert exc_info.value.code == "response_invalid_encoding"


def test_deep_json_has_stable_worker_error(monkeypatch):
    _post(monkeypatch, (b"[" * 20_000) + b"0" + (b"]" * 20_000))

    with pytest.raises(WorkerExecutionError) as exc_info:
        _post_json("https://llm.example/v1/chat/completions", {}, {}, 1)

    assert exc_info.value.code == "response_invalid_json"


def test_http_error_does_not_read_error_body(monkeypatch):
    class _ErrorBody:
        def read(self, *_args, **_kwargs):
            raise AssertionError("HTTP error body must not be read")

        def close(self):
            pass

    error = urllib.error.HTTPError(
        "https://llm.example/v1/chat/completions", 413, "too large", {}, _ErrorBody()
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )

    with pytest.raises(ProviderError) as exc_info:
        _post_json("https://llm.example/v1/chat/completions", {}, {}, 1)

    assert getattr(exc_info.value, "status_code") == 413
