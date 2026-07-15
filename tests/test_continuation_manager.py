"""Regression tests for continuation terminal decisions."""

from aigineering.core.budget_manager import BudgetManager
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.continuation_manager import (
    ContinuationManager,
    _tool_observation_succeeded,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.methods import method_contract
from aigineering.core.signing import Ed25519Signer
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.core.trace_manager import TraceManager
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Asset, Contract


def test_failed_tool_observation_does_not_authorize_continuation():
    observation = Asset(
        id="obs-failed",
        name="_tool_obs_child",
        content='{"ok": false, "error": "descriptor missing"}',
    )

    assert _tool_observation_succeeded([observation]) is False


def test_successful_tool_observation_authorizes_continuation():
    observation = Asset(
        id="obs-success",
        name="_tool_obs_child",
        content='{"ok": true, "result": "value"}',
    )

    assert _tool_observation_succeeded([observation]) is True


def test_missing_or_malformed_observation_fails_closed():
    malformed = Asset(id="obs-bad", name="_tool_obs_child", content="not-json")

    assert _tool_observation_succeeded([]) is False
    assert _tool_observation_succeeded([malformed]) is False


def test_continuation_manager_publishes_through_registered_plugin_candidate():
    store = MemoryStore()
    trace = MemoryTraceStore()
    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:continuation.publish.v1",
        "continuation-1",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "contract.publish.protected"),
    )
    genesis = create_genesis_manifest(
        "continuation-runtime", (actor,), "policy:continuation-runtime"
    )
    initialize_genesis(store, genesis)
    publisher = CandidatePublisher(store, trace, genesis, actor, signer)
    parent_fields = {
        "name": "root",
        "description": "Use a tool and finish the report.",
        "inputs": (),
        "outputs": ("report",),
        "activation": "",
        "budget": 4,
        "tool_scope": ("lookup",),
        "labels": (),
        "origin": "human",
    }
    parent = Contract(id=hash_contract_v3(**parent_fields), **parent_fields)
    source = method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    setup = publisher.publish(
        (
            contract_declaration_effect(parent),
            contract_declaration_effect(source),
        ),
        idempotency_key="continuation-setup",
    )
    assert setup.accepted is True
    budget = BudgetManager()
    budget.initialize(parent.id, parent.budget)
    manager = ContinuationManager(
        store=store,
        budget_mgr=budget,
        trace_mgr=TraceManager(trace),
        method_registry=None,
        completed=set(),
        suspended={parent.id},
        method_scheduled=set(),
        method_context={},
        candidate_publishers=CandidatePublisherRegistry(
            (("continuation.publish.v1", publisher),)
        ),
    )
    observation = Asset(
        id="obs-success",
        name=f"_tool_obs_{parent.id}",
        content='{"ok": true, "result": "value"}',
    )

    manager.schedule_continuation_contract(parent, source, [observation])

    continuations = [
        contract
        for contract in store.get_all_contracts()
        if contract.origin == "continuation"
    ]
    assert len(continuations) == 1
    receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("actor_id") == actor.actor_id
    ]
    assert len(receipts) == 2
    assert trace.get_by_event_type("method_continuation_scheduled")
