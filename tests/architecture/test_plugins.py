"""Plugins produce testable ordinary effects; they never mutate a Store."""

from __future__ import annotations

import json

from aigineering.core.candidate_publisher import publish_effects
from aigineering.core.ids import (
    hash_asset_content,
    hash_asset_definition,
    hash_contract_v3,
)
from aigineering.core.signing import Ed25519Signer
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.plugins import PlanningExpansionPlugin, PluginRequest, TaskPlugin
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
