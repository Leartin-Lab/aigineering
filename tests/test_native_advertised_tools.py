"""Native provider calls must name an exact function from the current request."""

from __future__ import annotations

import shutil

import pytest

from aigineering.agent.llm import LLMWorker
from aigineering.agent.worker import WorkerExecutionError
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
)
from conftest import candidate_runtime, hosted_worker


def _function(name: str, arguments: str = "{}") -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _response(*calls: dict, completion_tokens: int = 7) -> dict:
    return {
        "choices": [{"message": {"tool_calls": list(calls)}}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": completion_tokens,
            "total_tokens": 11 + completion_tokens,
        },
    }


def _definition(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "additionalProperties": True},
        },
    }


def _worker(response: dict, definitions: tuple[str, ...], *, seen=None) -> LLMWorker:
    def transport(_url, _headers, payload):
        if seen is not None:
            seen.update(payload)
        return response

    return LLMWorker(
        model="fixture-model",
        max_retries=0,
        capabilities=frozenset({"tool_calling"}),
        tool_definitions=[_definition(name) for name in definitions],
        transport=transport,
    )


def test_only_names_in_actual_scoped_request_map_to_method_actions():
    sent: dict = {}
    worker = _worker(
        _response(_function("tool", '{"q":"opaque"}'), _function("search", "{}")),
        ("tool", "search", "admin"),
        seen=sent,
    )
    contract = Contract(id="task:native-valid", tool_scope=("tool", "search"))

    candidate = worker.invoke(contract, [])

    assert [item["function"]["name"] for item in sent["tools"]] == ["tool", "search"]
    assert candidate.raw_output.startswith("/parallel_tool ")
    assert candidate.parsed_action["type"] == "parallel_tool"
    assert deep_thaw(candidate.parsed_action["payload"]["calls"]) == [
        {"name": "tool", "args": {"q": "opaque"}},
        {"name": "search", "args": {}},
    ]


@pytest.mark.parametrize(
    ("contract_scope", "configured", "returned_name", "arguments"),
    [
        ((), ("search",), "search", "{}"),  # unsolicited; no tools were sent
        (("search",), ("search", "admin"), "unknown", "not-json"),
        (("search",), ("search", "admin"), "admin", "{}"),
    ],
    ids=("unsolicited", "unknown-name-before-arguments", "configured-out-of-scope"),
)
def test_native_calls_outside_actual_request_fail_with_usage(
    contract_scope, configured, returned_name, arguments
):
    sent: dict = {}
    worker = _worker(
        _response(_function(returned_name, arguments), completion_tokens=13),
        configured,
        seen=sent,
    )
    contract = Contract(id="task:native-rejected", tool_scope=contract_scope)

    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(contract, [])

    assert raised.value.code == "tool_call_not_advertised"
    assert dict(raised.value.usage_metadata or {})["completion_tokens"] == 13
    if contract_scope:
        assert [item["function"]["name"] for item in sent["tools"]] == ["search"]
    else:
        assert "tools" not in sent


def test_json_object_mode_keeps_native_tool_response_priority():
    worker = LLMWorker(
        model="fixture-model",
        response_format="json_object",
        max_retries=0,
        transport=lambda *_args: _response(_function("search"), completion_tokens=5),
    )

    with pytest.raises(WorkerExecutionError) as raised:
        worker.invoke(Contract(id="task:json-object-native"), [])

    assert raised.value.code == "json_object_tool_call_response"
    assert dict(raised.value.usage_metadata or {})["completion_tokens"] == 5


def test_unadvertised_native_call_is_a_durable_claim_failure_and_keeps_cost(
    tmp_path,
):
    database = tmp_path / "native-tool-failure.sqlite3"
    store = SQLiteStore(str(database))
    setup = candidate_runtime(store)
    evidence = build_control_plane_asset(name="evidence", content="frozen input")
    setup.accept_asset(evidence)
    contract = setup.accept_contract(
        Contract(
            id="task:native-advertised-failure",
            name="native-attempt",
            inputs=("evidence",),
            outputs=("report",),
            activation="evidence",
            budget=5,
            tool_scope=("search",),
        )
    )
    worker = _worker(
        _response(_function("admin", "not-json"), completion_tokens=17),
        ("search", "admin"),
    )
    host = hosted_worker(
        store,
        worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        claim_candidate=host.claim_candidate(
            request_id="claim:native-advertised-failure", contract_id=contract.id
        ),
    )
    assert claimed is not None

    with pytest.raises(WorkerInvocationError, match="claim was released"):
        execute_claimed_package(claimed, host, store)

    records = tuple(record for _, record in store.scan_runtime_records())
    failures = [
        record
        for record in records
        if record.record_type == "worker.invocation_failed"
        and record.payload.get("contract_id") == contract.id
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.payload["category"] == "worker_error:tool_call_not_advertised"
    assert failure.payload["usage_metadata"]["completion_tokens"] == 17
    assert store.get_claim(contract.id)["status"] == "released"
    releases = [
        record
        for record in records
        if record.record_type == "claim.released"
        and record.payload.get("claim_id") == failure.payload["claim_id"]
    ]
    assert len(releases) == 1
    assert failure.id in releases[0].causal_parents

    grants = [
        record
        for record in records
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == contract.id
    ]
    extinguished = [
        record
        for record in records
        if record.record_type == "allowance.extinguished"
        and record.payload.get("contract_id") == contract.id
    ]
    assert len(grants) == len(extinguished) == 1
    assert extinguished[0].payload["amount"] == grants[0].payload["amount"] == 5
    assert extinguished[0].payload["reason"] == "worker.invocation_failed"

    assert store.get_assets_by_name("report") == []
    children = [
        task for task in store.get_all_contracts() if task.parent_id == contract.id
    ]
    assert children == []
    digest = store.runtime_materialization_digest()
    store.close()

    reopened = SQLiteStore(str(database))
    assert reopened.get_claim(contract.id)["status"] == "released"
    reopened_failure = reopened.scan_runtime_records(
        record_type="worker.invocation_failed"
    )
    assert [record.payload for _, record in reopened_failure] == [failure.payload]
    reopened.close()

    copied_database = tmp_path / "native-tool-failure-rebuild.sqlite3"
    shutil.copy2(database, copied_database)
    rebuilt = SQLiteStore(str(copied_database))
    assert rebuilt.runtime_materialization_digest() == digest
    assert rebuilt.rebuild_runtime_materializations(strict=True) == digest
    rebuilt.close()
