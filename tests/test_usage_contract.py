"""Shared usage metadata contract and signed-envelope boundary tests."""

import json
from pathlib import Path
from decimal import Decimal

import pytest

from conftest import hosted_worker
from aigineering.core.task_productivity import _usage_projection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.usage import (
    normalize_usage_metadata,
    validate_usage_metadata,
)
from aigineering.protocol.types import Candidate, Contract, TraceEntry
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
)
from aigineering.agent.worker import WorkerExecutionError, WorkerHost

_VECTORS = Path(__file__).parent.parent / "conformance/v0.5.11/usage-vectors.json"


def test_usage_normalization_is_allowlisted_and_deterministically_ordered():
    normalized = normalize_usage_metadata(
        {
            "retryable": False,
            "total_tokens": 0,
            "model": "fixture",
            "duration_ms": 4,
            "unknown": "credential-like",
            "prompt_tokens": True,
            "network": False,
        }
    )

    assert normalized == {
        "total_tokens": 0,
        "model": "fixture",
        "duration_ms": 4,
        "retryable": False,
        "network": False,
    }
    assert list(normalized) == [
        "total_tokens",
        "model",
        "duration_ms",
        "retryable",
        "network",
    ]


def test_language_neutral_usage_vectors():
    vectors = json.loads(_VECTORS.read_text(encoding="utf-8"))
    for case in vectors["cases"]:
        if case["valid"]:
            assert normalize_usage_metadata(case["input"]) == case["normalized"]
            validate_usage_metadata(case["input"])
        else:
            with pytest.raises(ValueError):
                validate_usage_metadata(case["input"])


def test_usage_normalization_distinguishes_absent_empty_and_zero():
    assert normalize_usage_metadata(None) is None
    assert normalize_usage_metadata({}) == {}
    assert normalize_usage_metadata({"total_tokens": 0}) == {"total_tokens": 0}


def test_usage_contract_keeps_known_tool_and_custom_adapter_fields():
    metadata = {
        "contract_id": "task:one",
        "tool": "lookup",
        "tool_version": "1.0.0",
        "duration_ms": 0,
        "result_bytes": 0,
        "error_type": "",
        "retryable": False,
        "adapter": "openalex",
        "network": False,
    }
    assert validate_usage_metadata(metadata) == metadata
    assert normalize_usage_metadata(metadata) == metadata


def test_productivity_omits_identity_only_llm_but_keeps_explicit_zero_and_tool():
    contract = Contract(id="task:usage-projection")
    traces = (
        TraceEntry(
            id="trace:identity-only",
            contract_id=contract.id,
            event_type="worker_invocation_failed",
            worker_id="llm:fixture",
            usage_metadata={
                "model": "fixture",
                "provider": "test",
                "worker_profile": "fixture-profile",
            },
        ),
        TraceEntry(
            id="trace:zero",
            contract_id=contract.id,
            event_type="candidate_committed",
            worker_id="llm:fixture",
            usage_metadata={"total_tokens": 0},
        ),
        TraceEntry(
            id="trace:tool",
            contract_id=contract.id,
            event_type="candidate_committed",
            worker_id="tool_worker:lookup",
            usage_metadata={"tool": "lookup"},
        ),
    )

    records, totals = _usage_projection(traces, [contract])

    assert [item["usage"] for item in records] == [
        {"total_tokens": 0},
        {"tool": "lookup"},
    ]
    assert totals == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


@pytest.mark.parametrize(
    "metadata",
    [
        {"unknown": "value"},
        {"total_tokens": True},
        {"duration_ms": -1},
        {"retryable": 0},
        {"provider": "bad\ud800value"},
    ],
)
def test_signed_usage_validation_rejects_invalid_metadata(metadata):
    with pytest.raises(ValueError):
        validate_usage_metadata(metadata)


def test_candidate_envelope_rejects_unknown_usage_instead_of_rewriting_it():
    with pytest.raises(ValueError, match="unknown fields"):
        CandidateEnvelope(
            contract_id="task:one",
            worker_id="worker:one",
            raw_output="output",
            usage_metadata={"model": "fixture", "arbitrary": "value"},
        )


class _MetadataWorker:
    worker_id = "worker:usage-contract"

    def __init__(self, metadata):
        self.metadata = metadata

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output='/exec {"outputs":{"report":"ok"}}',
            metadata=self.metadata,
        )


def test_custom_worker_usage_is_filtered_before_signing_and_commit(monkeypatch):
    store = SQLiteStore(":memory:")
    try:
        contract = Contract(id="task:custom-usage", outputs=("report",), budget=1)
        store.add_contract(contract)
        host = hosted_worker(
            store,
            _MetadataWorker(
                {
                    "model": "custom",
                    "total_tokens": 3,
                    "decimal": Decimal("1.25"),
                    "payload": b"secret-bytes",
                    "api_key": "secret-marker",
                }
            ),
        )
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=contract.id,
        )
        assert claimed is not None

        monkeypatch.setattr(
            "aigineering.runtime._ClaimLeaseKeeper.start", lambda self: None
        )
        monkeypatch.setattr(
            "aigineering.runtime._ClaimLeaseKeeper.stop", lambda self: None
        )
        result = execute_claimed_package(claimed, host, store)

        assert result["status"] == "accepted"
        receipt = next(
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("contract_id") == contract.id
        )
        assert receipt.payload["metadata"] == {"model": "custom", "total_tokens": 3}
        trace = next(
            entry
            for entry in store.get_trace_events(contract.id)
            if entry.event_type == "candidate_committed"
        )
        assert dict(trace.usage_metadata) == {"model": "custom", "total_tokens": 3}
        assert "secret-marker" not in json.dumps(deep_thaw(receipt.payload))
    finally:
        store.close()


def test_claim_renewal_failure_keeps_returned_candidate_usage(monkeypatch):
    store = SQLiteStore(":memory:")
    try:
        contract = Contract(id="task:renewal-usage", outputs=("report",), budget=1)
        store.add_contract(contract)

        class FailedKeeper:
            def __init__(self, claimed, _store):
                self.claimed = claimed

            def start(self):
                pass

            def stop(self):
                pass

            @property
            def failed(self):
                return True

        monkeypatch.setattr("aigineering.runtime._ClaimLeaseKeeper", FailedKeeper)
        worker = _MetadataWorker({"total_tokens": 17})
        claimed = claim_next_package(
            store, worker_id=worker.worker_id, contract_id=contract.id
        )
        assert claimed is not None

        with pytest.raises(WorkerInvocationError, match="renewal failed"):
            execute_claimed_package(claimed, worker, store)

        failure = store.scan_runtime_records(record_type="worker.invocation_failed")[
            -1
        ][1]
        assert failure.payload["usage_metadata"] == {"total_tokens": 17}
    finally:
        store.close()


def test_unsigned_adapter_failure_keeps_returned_candidate_usage(monkeypatch):
    store = SQLiteStore(":memory:")
    try:
        contract = Contract(id="task:unsigned-usage", outputs=("report",), budget=1)
        store.add_contract(contract)
        worker = _MetadataWorker({"tool": "lookup", "duration_ms": 0})
        claimed = claim_next_package(
            store, worker_id=worker.worker_id, contract_id=contract.id
        )
        assert claimed is not None
        monkeypatch.setattr(
            "aigineering.runtime._ClaimLeaseKeeper.start", lambda self: None
        )
        monkeypatch.setattr(
            "aigineering.runtime._ClaimLeaseKeeper.stop", lambda self: None
        )

        with pytest.raises(WorkerInvocationError, match="authenticated WorkerHost"):
            execute_claimed_package(claimed, worker, store)

        failure = store.scan_runtime_records(record_type="worker.invocation_failed")[
            -1
        ][1]
        assert failure.payload["usage_metadata"] == {"tool": "lookup", "duration_ms": 0}
    finally:
        store.close()


def test_empty_worker_error_usage_does_not_fall_back_to_candidate_usage(monkeypatch):
    store = SQLiteStore(":memory:")
    try:
        contract = Contract(id="task:empty-error-usage", outputs=("report",), budget=1)
        store.add_contract(contract)
        host = hosted_worker(store, _MetadataWorker({"total_tokens": 17}))
        claimed = claim_next_package(
            store, worker_id=host.worker_id, contract_id=contract.id
        )
        assert claimed is not None
        monkeypatch.setattr(
            "aigineering.runtime._ClaimLeaseKeeper.start", lambda self: None
        )
        monkeypatch.setattr(
            "aigineering.runtime._ClaimLeaseKeeper.stop", lambda self: None
        )

        def fail_signing(_self, *_args, **_kwargs):
            raise WorkerExecutionError(
                "usage_unavailable", "signed adapter rejected", usage_metadata={}
            )

        monkeypatch.setattr(WorkerHost, "sign_envelope", fail_signing)
        with pytest.raises(WorkerInvocationError, match="worker invocation failed"):
            execute_claimed_package(claimed, host, store)

        failure = store.scan_runtime_records(record_type="worker.invocation_failed")[
            -1
        ][1]
        assert failure.payload["usage_metadata"] == {}
        trace = store.get_by_contract(contract.id)[-1]
        assert dict(trace.usage_metadata) == {}
    finally:
        store.close()
