from __future__ import annotations

from aigineering.cli._task_batch import collect_task_batch, target_key
from aigineering.runtime_services import RedisSyncPending


def test_batch_collector_retains_typed_results_and_operation_keys():
    class RetainedRejection(Exception):
        def __init__(self):
            self.data = {
                "rejection_fact_id": "fact:reject",
                "fact_ids": ["fact:reject"],
                "idempotency_key": "retry:actual-owner-key",
            }
            self.revision = 19

    def invoke(contract_id, _key):
        if contract_id == "missing":
            raise LookupError("unknown task")
        if contract_id == "rejected":
            raise RetainedRejection()
        return (
            {
                "record_id": "fact:accepted",
                "candidate_id": "candidate:accepted",
                "fact_ids": ["fact:accepted"],
                "idempotency_key": "real-operation-key",
            },
            23,
        )

    result = collect_task_batch(
        ("missing", "rejected", "accepted"),
        action="retry",
        batch_key_base="batch-correlation",
        batch_field="batch_correlation_id",
        key_for_target=lambda _contract_id: None,
        invoke=invoke,
        expected_rejection_types=(RetainedRejection,),
        rejection_result=lambda exc: (exc.data, exc.revision),
        include_operation_revision=True,
    )
    rows = result.data["results"]
    assert result.data["batch_correlation_id"] == "batch-correlation"
    assert [row["outcome"] for row in rows] == ["not_found", "rejected", "accepted"]
    assert "idempotency_key" not in rows[0]
    assert rows[1]["fact_ids"] == ["fact:reject"]
    assert rows[1]["idempotency_key"] == "retry:actual-owner-key"
    assert rows[1]["revision"] == 19
    assert rows[2]["idempotency_key"] == "real-operation-key"
    assert rows[2]["revision"] == 23


def test_batch_collector_stops_when_rejection_lacks_exact_result():
    calls = []

    def invoke(contract_id, _key):
        calls.append(contract_id)
        if contract_id == "first":
            raise RedisSyncPending(accepted=False, revision=12)
        return ({"record_id": "unexpected"}, 13)

    result = collect_task_batch(
        ("first", "second"),
        action="retry",
        batch_key_base="base",
        invoke=invoke,
        include_operation_revision=True,
    )
    rows = result.data["results"]
    assert calls == ["first"]
    assert [row["outcome"] for row in rows] == ["outcome_unknown", "not_attempted"]
    assert rows[0]["candidate_accepted"] is False
    assert rows[0]["facts_retained"] is None
    assert rows[0]["relay_pending"] is True
    assert rows[0]["revision"] == 12
    assert rows[0]["idempotency_key"] == target_key("retry", "base", "first")
    assert "fact_ids" not in rows[0]
    assert rows[1]["command_attempted"] is False
    assert result.unexpected is not None
