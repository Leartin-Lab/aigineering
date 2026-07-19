"""Each planning stage is an ordinary independently testable Contract."""

from __future__ import annotations

import json

import pytest

from aigineering.agent.prompt import contract_prompt
from aigineering.core.candidate_publisher import publish_effects
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.plugins import (
    PluginRequest,
    StagedPlanningPlugin,
    StagedReplanningPlugin,
    TaskPlugin,
)
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest


def _parent() -> Contract:
    fields = {
        "name": "release_review",
        "description": "Produce a verified release review",
        "inputs": ("source",),
        "outputs": ("review",),
        "activation": "source",
        "budget": 8,
        "tool_scope": ("search",),
        "labels": ("behavior:careful",),
        "worker_pools": ("default",),
        "origin": "human",
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def test_staged_plan_proposes_three_atomic_ordinary_contracts():
    plugin = StagedPlanningPlugin()
    proposal = plugin.propose(
        PluginRequest(
            parent=_parent(),
            allowed_input_names=frozenset({"source"}),
            allowance=8,
        )
    )

    assert isinstance(plugin, TaskPlugin)
    assert [effect.effect_type for effect in proposal.effects] == [
        "contract.declare",
        "contract.declare",
        "contract.declare",
    ]
    assert len({effect.atomic_group for effect in proposal.effects}) == 1
    assert proposal.effects[0].atomic_group.startswith("plan:")

    stages = plugin.stages(
        PluginRequest(
            parent=_parent(),
            allowed_input_names=frozenset({"source"}),
            allowance=8,
        )
    )
    draft, dependencies, compile_contract = stages.contracts
    assert draft.parent_id == dependencies.parent_id == compile_contract.parent_id
    assert draft.outputs[0] in dependencies.inputs
    assert set(compile_contract.inputs) == {
        draft.outputs[0],
        dependencies.outputs[0],
    }
    assert compile_contract.activation == " AND ".join(compile_contract.inputs)
    assert compile_contract.outputs == (f"_plan_result_{_parent().id}",)
    assert all(contract.origin == "plugin" for contract in stages.contracts)
    assert all(
        contract.acceptance_policy["mode"] == "mechanical"
        for contract in stages.contracts
    )


@pytest.mark.parametrize(
    ("index", "label", "prompt_marker"),
    [
        (0, "plugin:plan.draft", "Planning draft protocol"),
        (1, "plugin:plan.dependencies", "Planning dependency protocol"),
        (2, "plugin:plan.compile", "Planning compiler protocol"),
    ],
)
def test_each_plan_stage_has_distinct_label_schema_and_expected_output(
    index: int, label: str, prompt_marker: str
):
    stages = (
        StagedPlanningPlugin()
        .stages(PluginRequest(parent=_parent(), allowance=8))
        .contracts
    )
    contract = stages[index]

    assert label in contract.labels
    assert prompt_marker in contract_prompt(contract, [])
    assert contract.outputs == tuple(contract.minting_authority)
    assert json.loads(contract.description)["stage"] in {
        "draft",
        "dependencies",
        "compile",
    }


def test_replan_stages_bind_invalidation_evidence_to_identity():
    evidence = Asset(id="failure:1", name="failure_evidence", content="bad premise")
    request = PluginRequest(parent=_parent(), assets=(evidence,), allowance=8)
    first = StagedReplanningPlugin().propose(request)
    changed = StagedReplanningPlugin().propose(
        PluginRequest(
            parent=_parent(),
            assets=(Asset(id="failure:2", name="failure_evidence", content="other"),),
            allowance=8,
        )
    )

    assert first.effects[0].atomic_group.startswith("replan:")
    assert first.effects[0].atomic_group != changed.effects[0].atomic_group
    assert (
        first.effects[0].payload["contract"]["id"]
        != changed.effects[0].payload["contract"]["id"]
    )


def test_staged_plan_rejects_insufficient_allowance_before_partial_proposal():
    with pytest.raises(ValueError, match="at least 3 allowance"):
        StagedPlanningPlugin().propose(PluginRequest(parent=_parent(), allowance=2))


def test_staged_plan_atomic_group_commits_through_candidate_boundary():
    proposal = StagedPlanningPlugin().propose(
        PluginRequest(parent=_parent(), allowance=8)
    )
    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:planning.staged.v1",
        "planning-staged-1",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "contract.publish.protected"),
    )
    genesis = create_genesis_manifest("staged-plan", (actor,), "policy:test")
    store = MemoryStore()

    decision = publish_effects(
        store,
        MemoryTraceStore(),
        genesis,
        actor,
        signer,
        proposal.effects,
        idempotency_key="staged-plan-1",
    )

    assert decision.accepted is True
    assert len(decision.contracts) == 3
    assert {contract.id for contract in store.get_all_contracts()} == {
        effect.payload["contract"]["id"] for effect in proposal.effects
    }
