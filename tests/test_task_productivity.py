"""Read-only task lineage productivity projections."""

from __future__ import annotations

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import hash_contract_current
from aigineering.core.store import MemoryStore
from aigineering.core.task_productivity import project_task_productivity
from aigineering.core.trace import create_entry
from aigineering.plugins.task_semantics import continuation_contract, method_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import trace_entry_to_dict


def _recovery_contract(parent):
    fields = {
        "name": f"{parent.name}.recover.1",
        "description": parent.description,
        "inputs": parent.inputs,
        "outputs": parent.outputs,
        "activation": parent.activation,
        "budget": 1,
        "tool_scope": parent.tool_scope,
        "labels": parent.labels,
        "context_asset_ids": parent.context_asset_ids,
        "worker_capabilities": parent.worker_capabilities,
        "worker_pools": parent.worker_pools,
        "delegation_capabilities": parent.delegation_capabilities,
        "delegation_pools": parent.delegation_pools,
        "origin": "recovery",
        "parent_id": parent.id,
        "minting_authority": parent.minting_authority,
        "sensitive_input_policy": parent.sensitive_input_policy,
        "acceptance_policy": parent.acceptance_policy,
    }
    from aigineering.protocol.types import Contract

    return Contract(id=hash_contract_current(**fields), **fields)


def _record(store, record_type, payload):
    store.append_runtime_record(create_runtime_record(record_type, payload))


def _trace_record(
    store, contract_id, event_type, *, worker_id=None, usage=None, rejected=None
):
    entry = create_entry(
        contract_id,
        event_type,
        worker_id=worker_id,
        usage_metadata=usage,
        rejected_fragments=rejected,
    )
    _record(store, "trace.recorded", {"trace": trace_entry_to_dict(entry)})


def test_productivity_projects_root_descendants_and_usage_without_mutation():
    store = MemoryStore()
    root = build_control_plane_contract(
        name="research",
        outputs=("report",),
        tool_scope=("lookup", "bad_lookup"),
        budget=8,
    )
    lookup = method_contract(
        root,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    bad_lookup = method_contract(
        root,
        WorkerAction(type="tool", payload={"name": "bad_lookup", "args": {}}),
    )
    continuation = continuation_contract(root, lookup, method="tool", budget=2)
    recovery = _recovery_contract(bad_lookup)
    for contract in (root, lookup, bad_lookup, continuation, recovery):
        store.add_contract(contract)

    _record(
        store,
        "lifecycle.terminal",
        {"contract_id": root.id, "terminal": "complete"},
    )
    _record(
        store,
        "lifecycle.terminal",
        {"contract_id": lookup.id, "terminal": "complete"},
    )
    _record(
        store,
        "lifecycle.terminal",
        {"contract_id": bad_lookup.id, "terminal": "failed"},
    )
    _record(
        store,
        "lifecycle.terminal",
        {"contract_id": recovery.id, "terminal": "failed"},
    )
    _record(
        store,
        "candidate.rejected",
        {"contract_id": recovery.id, "reason": "malformed output"},
    )
    _trace_record(
        store,
        root.id,
        "candidate_committed",
        worker_id="llm:fixture",
        usage={"model": "fixture", "prompt_tokens": 10, "completion_tokens": 5},
    )
    _trace_record(
        store,
        lookup.id,
        "candidate_committed",
        worker_id="tool_worker:lookup",
        usage={"tool": "lookup"},
    )
    _trace_record(
        store,
        root.id,
        "method_continuation_scheduled",
        worker_id="runtime",
    )

    revision = store.get_runtime_revision()
    result = project_task_productivity(root, store)

    assert store.get_runtime_revision() == revision
    assert result["root_contract_id"] == root.id
    assert result["contract_count"] == 5
    assert result["contract_statuses"] == {
        "active": 1,
        "complete": 2,
        "failed": 2,
    }
    assert result["terminal_count"] == 4
    assert result["terminal_statuses"] == {"complete": 2, "failed": 2}
    # The recovery contract is itself a tool attempt and remains visible as
    # a failed call; continuation is counted separately because it is not a
    # method contract.
    assert result["tool_calls"]["total"] == 3
    assert result["tool_calls"]["succeeded"] == 1
    assert result["tool_calls"]["failed"] == 2
    assert result["tool_calls"]["pending"] == 0
    assert result["continuation_count"] == 1
    assert result["continuation_scheduled_count"] == 1
    assert result["recovery_count"] == 1
    assert result["rejection_count"] == 1
    assert result["token_totals"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert {item["kind"] for item in result["usage_records"]} == {"llm", "tool"}


def test_productivity_excludes_unrelated_contracts_and_handles_orphans():
    store = MemoryStore()
    root = build_control_plane_contract(name="root", outputs=("result",))
    unrelated = build_control_plane_contract(name="unrelated", outputs=("other",))
    store.add_contract(root)
    store.add_contract(unrelated)
    _record(
        store,
        "lifecycle.terminal",
        {"contract_id": unrelated.id, "terminal": "complete"},
    )

    result = project_task_productivity(root, store)

    assert result["contract_ids"] == [root.id]
    assert result["contract_count"] == 1
    assert result["terminal_count"] == 0
    assert result["tool_calls"]["total"] == 0
