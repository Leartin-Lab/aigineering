from __future__ import annotations

import pytest

from aigineering.cli._task_batch import (
    collect_task_batch,
    exact_target_mapping,
    target_key,
    validate_task_ids,
)
from aigineering.runtime_services import RedisSyncPending
from aigineering.core.candidate_decision import CommitmentDecision
from aigineering.local_publication import verify_durable_rejection_member
from aigineering.protocol.runtime_record import create_runtime_record


def test_batch_validation_and_key_are_target_stable():
    ids = validate_task_ids(("task:a", "task:b"))
    assert target_key("block", "base", ids[0]) == target_key("block", "base", ids[0])
    assert target_key("block", "base", ids[0]) != target_key("block", "base", ids[1])
    with pytest.raises(ValueError, match="duplicates"):
        validate_task_ids(("task:a", "task:a"))
    with pytest.raises(ValueError, match="64"):
        validate_task_ids(tuple(f"task:{i}" for i in range(65)))
    with pytest.raises(ValueError, match="UTF-8"):
        validate_task_ids(("\ud800",))


def test_exact_target_map_requires_complete_unique_inputs():
    assert exact_target_mapping(
        ("a=hold-1", "b=hold-2"), ("a", "b"), multiple_per_target=False
    ) == {"a": ("hold-1",), "b": ("hold-2",)}
    with pytest.raises(ValueError, match="every target"):
        exact_target_mapping(("a=h",), ("a", "b"), multiple_per_target=False)
    with pytest.raises(ValueError, match="UTF-8"):
        exact_target_mapping(("a=\ud800",), ("a",), multiple_per_target=False)
    with pytest.raises(ValueError, match="duplicates"):
        exact_target_mapping(("a=h", "a=h"), ("a",), multiple_per_target=True)


def test_collector_preserves_pending_and_continues_known_rejection():
    def invoke(contract_id, key):
        if contract_id == "accepted-pending":
            exc = RedisSyncPending(
                accepted=True,
                data={
                    "contract_id": contract_id,
                    "candidate_id": "candidate:1",
                    "fact_ids": ["record:1"],
                    "idempotency_key": key,
                },
                revision=17,
            )
            raise exc
        if contract_id == "bad":
            raise ValueError("target-specific refusal")
        return (
            {
                "contract_id": contract_id,
                "record_id": "record:3",
                "candidate_id": "candidate:3",
                "idempotency_key": key,
            },
            21,
        )

    result = collect_task_batch(
        ("accepted-pending", "bad", "good"),
        action="block",
        batch_key_base="base",
        invoke=invoke,
    )
    rows = result.data["results"]
    assert [row["outcome"] for row in rows] == [
        "redis_sync_pending",
        "rejected",
        "accepted",
    ]
    assert rows[0]["candidate_accepted"] is True
    assert rows[0]["fact_ids"] == ["record:1"]
    assert rows[1]["outcome"] == "rejected"
    assert rows[2]["outcome"] == "accepted"
    assert result.revision == 21
    assert result.data["failed_count"] == 2


def test_accepted_pending_without_exact_result_is_unknown_not_unattempted():
    calls = []

    def invoke(contract_id, _key):
        calls.append(contract_id)
        raise RedisSyncPending(accepted=True, revision=9)

    result = collect_task_batch(
        ("task:a", "task:b"), action="cancel", batch_key_base="base", invoke=invoke
    )
    rows = result.data["results"]
    assert calls == ["task:a"]
    assert [row["outcome"] for row in rows] == ["outcome_unknown", "not_attempted"]
    assert rows[0]["candidate_accepted"] is True
    assert rows[0]["facts_retained"] is True
    assert "candidate_id" not in rows[0]
    assert "fact_ids" not in rows[0]
    assert rows[1]["candidate_accepted"] is None
    assert rows[1]["command_attempted"] is False
    assert rows[1]["idempotency_key"] == target_key("cancel", "base", "task:b")
    assert result.revision == 9


def test_authentication_rejection_transport_is_exact_metadata_without_receipt():
    rejected = create_runtime_record(
        "candidate.authentication_rejected",
        {
            "claimed_actor_id": "claimed:untrusted",
            "claimed_candidate_id": "candidate:untrusted",
            "reason": "key no longer authorized",
            "signature_kind": "ed25519",
        },
    )
    decision = CommitmentDecision(
        candidate_id="candidate:untrusted",
        accepted=False,
        runtime_records=(rejected,),
        trace_entries=(),
    )

    class Store:
        def get_runtime_record(self, record_id):
            return rejected if record_id == rejected.id else None

    assert verify_durable_rejection_member(Store(), decision).id == rejected.id


@pytest.mark.parametrize("error", (KeyError("missing key"), IndexError("bad index")))
def test_lookup_subclass_is_unknown_and_stops_remaining_targets(error):
    calls = []

    def invoke(contract_id, _key):
        calls.append(contract_id)
        if contract_id == "first":
            raise error
        return ({"contract_id": contract_id}, 1)

    result = collect_task_batch(
        ("first", "second"), action="block", batch_key_base="base", invoke=invoke
    )
    assert calls == ["first"]
    assert [row["outcome"] for row in result.data["results"]] == [
        "outcome_unknown",
        "not_attempted",
    ]
    assert result.data["results"][0]["error"]["exception_class"] == type(error).__name__
    assert result.unexpected is error
