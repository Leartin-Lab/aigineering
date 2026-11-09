"""Plugins produce testable ordinary effects; they never mutate a Store."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from aigineering.core.candidate_publisher import (
    publish_effects,
)
from aigineering.core.ids import (
    hash_asset_content,
    hash_asset_definition,
    hash_contract_current,
    hash_contract_v3,
)
from aigineering.core.signing import Ed25519Signer
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.plugins.task_semantics import method_contract, method_payload
from aigineering.plugins import (
    PlanningExpansionPlugin,
    PluginRequest,
    TaskDelegationPlugin,
    TaskPlugin,
)
from aigineering.plugins.planning import (
    PlanningCompileError,
    compile_planning_blueprint,
)
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import contract_from_dict


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
                    "description": "Extract evidence from the source.",
                    "inputs": ["source"],
                    "outputs": ["evidence"],
                    "activation": "source",
                    "budget": 2,
                },
                {
                    "name": "write",
                    "description": "Write the final report from the evidence.",
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
    parent_decision = publish_effects(
        store,
        trace,
        genesis,
        actor,
        signer,
        (contract_declaration_effect(_parent()),),
        idempotency_key="planning-parent",
    )
    assert parent_decision.accepted is True

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
        "deliver_report",
        "research",
        "write",
    }


def test_planning_plugin_rejects_invalid_activation_as_one_atomic_fanout():
    content = json.dumps(
        {
            "contracts": [
                {
                    "name": "valid",
                    "description": "Extract valid evidence.",
                    "inputs": ["source"],
                    "outputs": ["evidence"],
                    "activation": "source",
                },
                {
                    "name": "invalid",
                    "description": "Write the report.",
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


def test_planning_plugin_rejects_invalid_scaffold_as_one_atomic_fanout():
    content = json.dumps(
        {
            "reason": "missing data-flow stage",
            "goal_outline": "produce final report",
            "intermediate_assets": [],
            "step_1_tasks": [{"name": "draft", "description": "Draft."}],
            "step_2_data_flow": [],
            "step_3_activation": [
                {"task_name": "draft", "expression": "source", "depends_on": ["source"]}
            ],
        },
        sort_keys=True,
    )
    proposal = PlanningExpansionPlugin().propose(
        PluginRequest(
            parent=_parent(),
            assets=(Asset(id="invalid-scaffold", name="plan", content=content),),
            allowed_input_names=frozenset({"source"}),
            allowance=4,
        )
    )

    assert proposal.effects == ()
    assert any(
        rejection.get("action") == "scaffold_rejected"
        for rejection in proposal.rejections
    )


def test_compile_error_exposes_only_stable_rejection_fields():
    contract = replace(
        _parent(),
        description=json.dumps({"allowed_inputs": ["source"]}),
        budget=2,
    )
    blueprint = json.dumps(
        {
            "contracts": [
                {
                    "name": "empty",
                    "description": "",
                    "inputs": ["source"],
                    "outputs": [],
                    "budget": 1,
                }
            ]
        }
    )

    with pytest.raises(PlanningCompileError) as raised:
        compile_planning_blueprint(
            contract,
            {"planning_blueprint": blueprint},
            allowance=2,
        )

    assert raised.value.fields == ("description", "output_recommitment")


def test_compile_rejects_labels_outside_parent_scope_before_commitment():
    contract = replace(
        _parent(),
        description=json.dumps({"allowed_inputs": ["source"]}),
        labels=("plugin:plan.compile",),
        budget=1,
    )
    blueprint = json.dumps(
        {
            "contracts": [
                {
                    "name": "finish",
                    "description": "Produce the required output.",
                    "inputs": ["source"],
                    "outputs": ["final_report"],
                    "activation": "source",
                    "budget": 1,
                    "tool_scope": [],
                    "labels": ["invented"],
                }
            ]
        }
    )

    with pytest.raises(PlanningCompileError) as raised:
        compile_planning_blueprint(
            contract,
            {"planning_blueprint": blueprint},
            allowance=1,
        )

    assert raised.value.fields == ("labels", "output_recommitment")
    assert "not a subset of parent labels" in str(raised.value)


def test_compile_requires_verifier_child_for_independent_parent_acceptance():
    description = {
        "allowed_inputs": ["source"],
        "parent_acceptance_policy": {
            "mode": "independent",
            "verifier_capabilities": ["literature.verify"],
        },
    }
    contract = replace(
        _parent(),
        parent_id="task:root",
        description=json.dumps(description),
        worker_capabilities=("literature.verify",),
        delegation_capabilities=("literature.verify",),
        budget=3,
    )
    producer = {
        "name": "produce",
        "description": "Produce the report.",
        "inputs": ["source"],
        "outputs": ["final_report"],
        "activation": "source",
        "budget": 1,
        "tool_scope": [],
        "labels": [],
        "capability_needs": [],
        "pool_needs": [],
        "delegation_capabilities": [],
        "delegation_pools": [],
    }

    with pytest.raises(PlanningCompileError) as raised:
        compile_planning_blueprint(
            contract,
            {"planning_blueprint": json.dumps({"contracts": [producer]})},
            allowance=3,
        )
    assert raised.value.fields == ("acceptance_policy",)

    verifier = {
        "name": "verify",
        "description": "Attest the exact final report Asset.",
        "inputs": ["final_report"],
        "outputs": ["verification_receipt"],
        "activation": "final_report",
        "budget": 1,
        "tool_scope": [],
        "labels": [],
        "capability_needs": ["literature.verify"],
        "pool_needs": [],
        "delegation_capabilities": [],
        "delegation_pools": [],
    }
    effects = compile_planning_blueprint(
        contract,
        {"planning_blueprint": json.dumps({"contracts": [producer, verifier]})},
        allowance=3,
    )
    assert len(effects) == 2


def test_contract_v5_tool_child_requires_explicit_execution_capability():
    fields = {
        "name": "compile",
        "description": json.dumps({"allowed_inputs": ["source"]}),
        "inputs": ("source",),
        "outputs": ("final_report",),
        "activation": "source",
        "budget": 2,
        "tool_scope": ("read",),
        "labels": ("plugin:plan.compile",),
        "delegation_capabilities": ("text.retrieve",),
        "origin": "plugin",
    }
    contract = Contract(id=hash_contract_current(**fields), **fields)
    blueprint = {
        "contracts": [
            {
                "name": "retrieve",
                "description": "Retrieve the report source.",
                "inputs": ["source"],
                "outputs": ["final_report"],
                "activation": "source",
                "budget": 2,
                "tool_scope": ["read"],
                "labels": [],
                "capability_needs": [],
                "pool_needs": [],
                "delegation_capabilities": [],
                "delegation_pools": [],
            }
        ]
    }

    with pytest.raises(PlanningCompileError) as raised:
        compile_planning_blueprint(
            contract,
            {"planning_blueprint": json.dumps(blueprint)},
            allowance=2,
        )

    assert "capability_needs" in raised.value.fields


def test_compile_binds_skill_label_and_specialized_execution_requirement():
    skill = Asset(
        id="asset:skill-extract-v1",
        name="skill:literature.extract",
        content="Extract source-bound evidence cards.",
        trust_tier="configured",
    )
    fields = {
        "name": "compile",
        "description": json.dumps({"allowed_inputs": ["source"]}),
        "inputs": ("source",),
        "outputs": ("final_report",),
        "activation": "source",
        "budget": 2,
        "tool_scope": (),
        "labels": ("plugin:plan.compile", skill.name),
        "context_asset_ids": (skill.id,),
        "worker_capabilities": ("planning.compile",),
        "worker_pools": ("orchestrator",),
        "delegation_capabilities": ("text.extract", "reasoning.deep"),
        "delegation_pools": ("economy", "advanced"),
        "origin": "plugin",
    }
    contract = Contract(id=hash_contract_current(**fields), **fields)
    blueprint = json.dumps(
        {
            "contracts": [
                {
                    "name": "extract",
                    "description": "Extract evidence from the source.",
                    "inputs": ["source"],
                    "outputs": ["final_report"],
                    "activation": "source",
                    "budget": 1,
                    "tool_scope": [],
                    "labels": [skill.name],
                    "capability_needs": ["text.extract"],
                    "pool_needs": ["economy"],
                }
            ]
        }
    )

    effects = compile_planning_blueprint(
        contract,
        {"planning_blueprint": blueprint},
        allowance=2,
        context_assets=(skill,),
    )
    child = contract_from_dict(effects[0].payload["contract"])

    assert child.worker_capabilities == ("text.extract",)
    assert child.worker_pools == ("economy",)
    assert child.labels == (skill.name,)
    assert child.context_asset_ids == (skill.id,)
    assert child.id.startswith("task:v4:")


def test_compile_rejects_execution_requirement_outside_delegation_scope():
    fields = {
        "name": "compile",
        "description": json.dumps({"allowed_inputs": ["source"]}),
        "inputs": ("source",),
        "outputs": ("final_report",),
        "activation": "source",
        "budget": 1,
        "tool_scope": (),
        "labels": ("plugin:plan.compile",),
        "delegation_capabilities": ("text.extract",),
        "origin": "plugin",
    }
    contract = Contract(id=hash_contract_current(**fields), **fields)
    blueprint = json.dumps(
        {
            "contracts": [
                {
                    "name": "synthesize",
                    "description": "Synthesize the final report.",
                    "inputs": ["source"],
                    "outputs": ["final_report"],
                    "activation": "source",
                    "budget": 1,
                    "tool_scope": [],
                    "labels": [],
                    "capability_needs": ["reasoning.deep"],
                }
            ]
        }
    )

    with pytest.raises(PlanningCompileError) as raised:
        compile_planning_blueprint(
            contract,
            {"planning_blueprint": blueprint},
            allowance=1,
        )

    assert "capability_needs" in raised.value.fields


def test_compile_allows_optional_tool_permission_with_single_task_allowance():
    fields = {
        "name": "compile",
        "description": json.dumps({"allowed_inputs": ["source"]}),
        "inputs": ("source",),
        "outputs": ("final_report",),
        "activation": "source",
        "budget": 2,
        "tool_scope": ("lookup",),
        "labels": ("plugin:plan.compile",),
        "origin": "plugin",
    }
    contract = Contract(id=hash_contract_current(**fields), **fields)
    blueprint = json.dumps(
        {
            "contracts": [
                {
                    "name": "lookup",
                    "description": "Produce the report from the source; lookup is optional.",
                    "inputs": ["source"],
                    "outputs": ["final_report"],
                    "activation": "source",
                    "budget": 1,
                    "tool_scope": ["lookup"],
                    "labels": [],
                }
            ]
        }
    )

    effects = compile_planning_blueprint(
        contract,
        {"planning_blueprint": blueprint},
        allowance=2,
    )

    assert len(effects) == 1
    child = Contract(**effects[0].payload["contract"])
    assert child.budget == 1
    assert child.tool_scope == ("lookup",)
    assert child.outputs == ("final_report",)


def test_continuation_contract_is_an_ordinary_declaration_value():
    from aigineering.plugins.task_semantics import continuation_contract

    parent = _parent()
    source = method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    continuation = continuation_contract(parent, source, method="tool", budget=3)
    assert continuation.origin == "continuation"
    assert continuation.parent_id == parent.id
    assert continuation.budget == 3
    assert continuation.outputs == parent.outputs


def test_parallel_tool_method_compiles_independent_tasks_and_boolean_join():
    fields = {
        "name": "research",
        "description": "Research two independent topics.",
        "inputs": ("source",),
        "outputs": ("final_report",),
        "activation": "source",
        "budget": 4,
        "tool_scope": ("search", "lookup"),
        "labels": (),
        "worker_capabilities": ("reasoning.deep",),
        "worker_pools": ("advanced",),
        "delegation_capabilities": ("text.extract",),
        "delegation_pools": ("economy",),
        "origin": "human",
    }
    parent = Contract(id=hash_contract_current(**fields), **fields)
    action = WorkerAction(
        type="parallel_tool",
        payload={
            "calls": [
                {"id": "a", "name": "search", "args": {"q": "alpha"}},
                {"id": "b", "name": "lookup", "args": {"q": "beta"}},
            ],
            "join": "all",
        },
    )

    proposal = TaskDelegationPlugin().propose_claimed(
        parent, action, allowance=parent.budget
    )
    contracts = [
        contract_from_dict(effect.payload["contract"]) for effect in proposal.effects
    ]
    tools, continuation = contracts[:-1], contracts[-1]

    assert len(tools) == 2
    assert all(item.parent_id == parent.id for item in contracts)
    assert {item.worker_capabilities for item in tools} == {
        ("tool-execution", "tool:search"),
        ("tool-execution", "tool:lookup"),
    }
    assert all(item.worker_pools == () for item in tools)
    assert {json.loads(item.description)["method"] for item in tools} == {
        "parallel_tool_item"
    }
    assert set(continuation.inputs) == {item.outputs[0] for item in tools}
    assert continuation.activation == " AND ".join(continuation.inputs)
    assert continuation.outputs == parent.outputs
    assert all(item.budget == 1 for item in tools)
    assert continuation.budget == 1
    assert sum(item.budget for item in contracts) == parent.budget - 1


@pytest.mark.parametrize("action_type", ["tool", "fail"])
def test_claimed_action_plugin_proposes_ordinary_task_without_store(action_type):
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

    proposal = TaskDelegationPlugin().propose_claimed(
        parent,
        WorkerAction(type=action_type, payload=payload),
        allowance=parent.budget,
    )
    child = contract_from_dict(proposal.effects[0].payload["contract"])

    assert proposal.effects[0].effect_type == "contract.declare"
    assert child.parent_id == parent.id
    assert child.origin == "system"
    assert f"plugin:{action_type}" in child.labels


def test_claimed_action_plugin_proposes_retry_descendant():
    parent = _parent()

    proposal = TaskDelegationPlugin().propose_claimed(
        parent,
        WorkerAction(type="retry", payload={"reason": "retry"}),
        allowance=parent.budget,
    )
    child = contract_from_dict(proposal.effects[0].payload["contract"])

    assert child.origin == "retry"
    assert child.parent_id == parent.id
    assert child.outputs == parent.outputs


def test_claimed_action_plugin_rejects_unknown_action_without_fallback():
    with pytest.raises(ValueError, match="unsupported claimed task action"):
        TaskDelegationPlugin().propose_claimed(
            _parent(),
            WorkerAction(type="unknown", payload={}),
            allowance=1,
        )


def test_tool_action_is_compiled_as_an_ordinary_method_task():
    parent = replace(_parent(), tool_scope=("lookup",))
    proposal = TaskDelegationPlugin().propose_claimed(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
        allowance=parent.budget,
    )
    assert all(effect.effect_type == "contract.declare" for effect in proposal.effects)
    declared = [
        contract_from_dict(effect.payload["contract"]) for effect in proposal.effects
    ]
    assert any(method_payload(item).get("method") == "tool" for item in declared)
    assert any(item.origin == "continuation" for item in declared)


def test_plugins_do_not_expose_postcompletion_lifecycle_controller():
    from pathlib import Path

    plugin_root = Path("src/aigineering/plugins")
    for module in (
        "completion.py",
        "completion_projection.py",
        "fail_completion.py",
        "planning_completion.py",
        "tool_completion.py",
    ):
        assert not (plugin_root / module).exists()

    exports = (plugin_root / "__init__.py").read_text()
    for export in (
        "CompletionRegistry",
        "TaskCompletionContext",
        "TaskCompletionProjector",
        "FailCompletionPlugin",
    ):
        assert export not in exports
