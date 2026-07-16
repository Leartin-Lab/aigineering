"""Plugins produce testable ordinary effects; they never mutate a Store."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
    publish_effects,
)
from aigineering.core.ids import (
    hash_asset_content,
    hash_asset_definition,
    hash_contract_v3,
)
from aigineering.core.signing import Ed25519Signer
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.core.methods import method_contract
from aigineering.plugins import (
    ContinuationTaskPlugin,
    PlanningExpansionPlugin,
    PluginRequest,
    TaskDelegationPlugin,
    TaskPlugin,
    ToolCompletionPlugin,
)
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.types import Asset, Contract


def _parent() -> Contract:
    fields = {
        "name": "deliver_report",
        "description": "Deliver a verified report",
        "inputs": ("source",),
        "outputs": ("final_report",),
        "activation": "source",
        "budget": 4,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _plan_asset() -> Asset:
    content = json.dumps(
        {
            "contracts": [
                {
                    "name": "research",
                    "inputs": ["source"],
                    "outputs": ["evidence"],
                    "activation": "source",
                    "budget": 2,
                },
                {
                    "name": "write",
                    "inputs": ["evidence"],
                    "outputs": ["final_report"],
                    "activation": "evidence",
                    "budget": 2,
                },
            ]
        },
        sort_keys=True,
    )
    return Asset(
        id=hash_asset_content("plan", content),
        name="plan",
        content=content,
        definition_hash=hash_asset_definition("plan"),
        content_hash=hash_asset_content("plan", content),
    )


def test_planning_plugin_proposes_contained_tasks_without_store_access():
    plugin = PlanningExpansionPlugin()

    proposal = plugin.propose(
        PluginRequest(
            parent=_parent(),
            assets=(_plan_asset(),),
            allowed_input_names=frozenset({"source"}),
            allowance=4,
        )
    )

    assert isinstance(plugin, TaskPlugin)
    assert [effect.effect_type for effect in proposal.effects] == [
        "contract.declare",
        "contract.declare",
    ]
    assert proposal.rejections == ()


def test_planning_plugin_fanout_commits_through_candidate_publisher():
    plugin = PlanningExpansionPlugin()
    proposal = plugin.propose(
        PluginRequest(
            parent=_parent(),
            assets=(_plan_asset(),),
            allowed_input_names=frozenset({"source"}),
            allowance=4,
        )
    )
    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:planning",
        "planning-1",
        signer.kind,
        signer.signer_id,
        ("contract.publish",),
    )
    genesis = create_genesis_manifest("plugin-test", (actor,), "policy:plugin-test")
    store = MemoryStore()
    trace = MemoryTraceStore()

    decision = publish_effects(
        store,
        trace,
        genesis,
        actor,
        signer,
        proposal.effects,
        idempotency_key="planning-fanout-1",
    )

    assert decision.accepted is True
    assert len(decision.contracts) == 2
    assert {contract.name for contract in store.get_all_contracts()} == {
        "research",
        "write",
    }


def test_planning_plugin_rejects_invalid_activation_as_one_atomic_fanout():
    content = json.dumps(
        {
            "contracts": [
                {
                    "name": "valid",
                    "inputs": ["source"],
                    "outputs": ["evidence"],
                    "activation": "source",
                },
                {
                    "name": "invalid",
                    "inputs": ["evidence", "source"],
                    "outputs": ["final_report"],
                    "activation": "evidence, source",
                },
            ]
        },
        sort_keys=True,
    )
    asset = Asset(id="invalid-plan", name="plan", content=content)

    proposal = PlanningExpansionPlugin().propose(
        PluginRequest(
            parent=_parent(),
            assets=(asset,),
            allowed_input_names=frozenset({"source"}),
            allowance=4,
        )
    )

    assert proposal.effects == ()
    assert any(
        rejection.get("field") == "activation" and rejection.get("recoverable") is True
        for rejection in proposal.rejections
    )


def test_continuation_plugin_proposes_one_ordinary_task_and_registry_is_explicit():
    parent = _parent()
    source = method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    plugin = ContinuationTaskPlugin()

    proposal = plugin.propose(PluginRequest(parent=parent, source=source, allowance=3))

    assert isinstance(plugin, TaskPlugin)
    assert len(proposal.effects) == 1
    assert proposal.effects[0].effect_type == "contract.declare"
    payload = proposal.effects[0].payload["contract"]
    assert payload["origin"] == "continuation"
    assert payload["parent_id"] == parent.id
    assert payload["budget"] == 3

    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:continuation.publish.v1",
        "continuation-1",
        signer.kind,
        signer.signer_id,
        ("contract.publish",),
    )
    genesis = create_genesis_manifest(
        "continuation-test", (actor,), "policy:continuation-test"
    )
    store = MemoryStore()
    trace = MemoryTraceStore()
    publisher = CandidatePublisher(store, trace, genesis, actor, signer)
    publishers = CandidatePublisherRegistry(((plugin.plugin_id, publisher),))

    decision = publishers.get(plugin.plugin_id).publish(
        proposal.effects,
        idempotency_key="continuation-1",
        causal_parents=(source.id,),
    )

    assert decision.accepted is True
    assert len(store.get_all_contracts()) == 1


@pytest.mark.parametrize("action_type", ["plan", "replan", "tool", "fail"])
def test_delegation_plugin_projects_each_method_task_without_store(action_type):
    parent = _parent()
    payload = (
        {"name": "lookup", "args": {}}
        if action_type == "tool"
        else {"reason": "test delegation"}
    )
    if action_type == "tool":
        parent = replace(
            parent,
            id=hash_contract_v3(
                name=parent.name,
                description=parent.description,
                inputs=parent.inputs,
                outputs=parent.outputs,
                activation=parent.activation,
                budget=parent.budget,
                tool_scope=("lookup",),
                labels=parent.labels,
                origin=parent.origin,
            ),
            tool_scope=("lookup",),
        )

    projection = TaskDelegationPlugin().project(
        parent,
        WorkerAction(type=action_type, payload=payload),
    )

    assert projection.child.parent_id == parent.id
    assert projection.child.origin == "system"
    assert f"method:{action_type}" in projection.child.labels
    assert projection.context_asset is not None
    assert projection.context_asset.name == f"_method_ctx_{parent.id}"
    assert projection.event_type == "task_delegated"


def test_delegation_plugin_projects_retry_as_an_independent_task():
    parent = _parent()

    projection = TaskDelegationPlugin().project(
        parent,
        WorkerAction(type="retry", payload={"reason": "retry"}),
    )

    assert projection.child.origin == "retry"
    assert projection.child.parent_id == parent.parent_id
    assert projection.child.outputs == parent.outputs
    assert projection.context_asset is None
    assert projection.event_type == "retry_created"


def test_delegation_plugin_rejects_unknown_action_without_handler_fallback():
    with pytest.raises(ValueError, match="unsupported task delegation action"):
        TaskDelegationPlugin().project(
            _parent(),
            WorkerAction(type="unknown", payload={}),
        )


def test_tool_completion_plugin_requires_declared_worker_observation():
    parent = replace(_parent(), tool_scope=("lookup",))
    contract = method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    observation = Asset(
        id="tool-observation",
        name=contract.outputs[0],
        content='{"ok":true,"result":"value"}',
    )
    plugin = ToolCompletionPlugin()

    assert plugin.handle_completion(None, contract, [observation]) is True
    assert plugin.handle_completion(None, contract, []) is False
