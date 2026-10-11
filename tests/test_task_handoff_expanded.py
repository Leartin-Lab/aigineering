"""Expanded handoff tree inventory behavior."""

from __future__ import annotations

import pytest

from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.domain import initialize_genesis
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.provenance import sign_asset
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry, trace_record_from_entry
from aigineering.plugins.handoff import (
    HANDOFF_EFFECTS,
    project_task_handoff_effect,
    project_task_handoffs,
)
from aigineering.plugins.handoff_tree import inventory_handoff_tree
from aigineering.plugins.task_successors import remaining_output_slots
from aigineering.cli.task_handoff import _human_handoff
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    task_dependency_link_effect,
    task_handoff_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract


def _contract(
    name: str,
    *,
    parent_id: str | None = None,
    outputs: tuple[str, ...] = (),
    inputs: tuple[str, ...] = (),
    worker_capabilities: tuple[str, ...] = (),
    acceptance_policy: dict | None = None,
    origin: str = "human",
):
    return contract_from_fields(
        name=name,
        description=name,
        parent_id=parent_id,
        inputs=inputs,
        outputs=outputs,
        activation="",
        budget=3,
        tool_scope=(),
        labels=(),
        worker_capabilities=worker_capabilities,
        origin=origin,
        acceptance_policy=acceptance_policy,
    )


def test_inventory_selects_live_descendants_and_repairs_satisfied_intermediate():
    source = _contract("source", outputs=("final",))
    repair = _contract("repair", parent_id=source.id, outputs=("draft",))
    leaf = _contract("leaf", parent_id=repair.id, outputs=("notes",))
    asset = Asset(
        id="asset:completed-draft",
        name="draft",
        content="finished",
        created_by=repair.id,
        origin="human",
    )
    asset_fact = create_runtime_record(
        "asset.committed", {"asset": {"id": asset.id}, "contract_id": repair.id}
    )
    context = EffectProjectionContext(
        contracts=(source, repair, leaf),
        assets=(asset,),
        runtime_records=(asset_fact,),
    )

    inventory = inventory_handoff_tree(source, context)

    assert inventory.cancelled == (leaf,)
    assert inventory.repairs == (repair,)
    assert inventory.repair_outputs == (
        (repair.id, (("draft", (asset.id,), (asset_fact.id,)),)),
    )
    assert inventory.preserved == ()


def test_completion_repair_terminal_and_link_bind_exact_output_proof():
    signer = Ed25519Signer()
    source = _contract("source", outputs=("final",))
    repair = _contract("repair", parent_id=source.id, outputs=("draft",))
    asset = Asset(
        id="asset:repair-proof",
        name="draft",
        content="complete draft",
        created_by=repair.id,
        origin="human",
    )
    asset_fact = create_runtime_record(
        "asset.committed", {"asset": {"id": asset.id}, "contract_id": repair.id}
    )
    effect = task_handoff_effect(source.id, "Continue source work")
    candidate = create_candidate_proposal(
        domain_id="genesis:handoff-test",
        actor_id="human:operator",
        key_id="operator-key",
        effects=(effect,),
        signer=signer,
        idempotency_key="repair-proof",
    )
    projection = project_task_handoff_effect(
        effect,
        candidate,
        "record:candidate-receipt",
        EffectProjectionContext(
            contracts=(source, repair), assets=(asset,), runtime_records=(asset_fact,)
        ),
    )

    repair_terminal = next(
        record
        for record in projection.records
        if record.record_type == "lifecycle.terminal"
        and record.payload["contract_id"] == repair.id
    )
    link = next(
        record
        for record in projection.records
        if record.record_type == "task.handed_off"
    )
    assert repair_terminal.payload["terminal"] == "complete"
    assert asset.id in repair_terminal.causal_parents
    assert asset_fact.id in repair_terminal.causal_parents
    assert link.payload["repair_record_ids"] == (repair_terminal.id,)
    assert link.payload["repair_output_proofs"][repair.id][0]["asset_ids"] == (
        asset.id,
    )
    assert link.payload["repair_output_proofs"][repair.id][0]["proof_record_ids"] == (
        asset_fact.id,
    )
    assert repair_terminal.id in link.causal_parents


def test_inventory_preserves_external_independent_verifier_branch():
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "rubric_asset_ids": [],
        "evidence_asset_ids": [],
        "required_attestations": 1,
        "verifier_capabilities": ["review"],
    }
    ancestor = _contract("ancestor", outputs=("final",), acceptance_policy=policy)
    source = _contract("source", parent_id=ancestor.id, outputs=("final",))
    verifier = _contract(
        "verifier",
        parent_id=source.id,
        inputs=("final",),
        worker_capabilities=("review",),
    )
    verifier_child = _contract("verifier-child", parent_id=verifier.id)
    context = EffectProjectionContext(
        contracts=(ancestor, source, verifier, verifier_child)
    )

    inventory = inventory_handoff_tree(source, context)

    assert inventory.cancelled == ()
    assert inventory.repairs == ()
    assert set(inventory.preserved) == {verifier.id, verifier_child.id}


def test_inventory_reports_exact_hold_and_legacy_trace_conflict_ids():
    source = _contract("source", outputs=("final",))
    child = _contract("child", parent_id=source.id)
    receipt = create_runtime_record("candidate.received", {"candidate_id": "c"})
    hold = create_runtime_record(
        "claim.hold.placed",
        {
            "contract_id": child.id,
            "reason": "pause",
            "actor_id": "human:operator",
            "key_id": "key",
            "candidate_id": "candidate",
        },
        causal_parents=(receipt.id,),
    )
    adverse = trace_record_from_entry(create_entry(child.id, "failed"))
    context = EffectProjectionContext(
        contracts=(source, child), runtime_records=(receipt, hold, adverse)
    )

    with pytest.raises(ValueError) as exc_info:
        inventory_handoff_tree(source, context)

    message = str(exc_info.value)
    assert child.id in message
    assert hold.id in message
    assert adverse.id in message


def test_preserved_verifier_history_and_hold_are_outside_transfer_blockers():
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "rubric_asset_ids": [],
        "evidence_asset_ids": [],
        "required_attestations": 1,
        "verifier_capabilities": ["review"],
    }
    ancestor = _contract("ancestor", outputs=("final",), acceptance_policy=policy)
    source = _contract("source", parent_id=ancestor.id, outputs=("final",))
    verifier = _contract(
        "verifier",
        parent_id=source.id,
        inputs=("final",),
        worker_capabilities=("review",),
    )
    receipt = create_runtime_record("candidate.received", {"candidate_id": "c"})
    hold = create_runtime_record(
        "claim.hold.placed",
        {
            "contract_id": verifier.id,
            "reason": "review pause",
            "actor_id": "human:operator",
            "key_id": "key",
            "candidate_id": "candidate",
        },
        causal_parents=(receipt.id,),
    )
    adverse = trace_record_from_entry(create_entry(verifier.id, "failed"))
    inventory = inventory_handoff_tree(
        source,
        EffectProjectionContext(
            contracts=(ancestor, source, verifier),
            runtime_records=(receipt, hold, adverse),
        ),
    )

    assert inventory.cancelled == ()
    assert inventory.preserved == (verifier.id,)


def test_empty_output_descendant_is_cancelled_not_repaired():
    source = _contract("source", outputs=("final",))
    child = _contract("empty", parent_id=source.id)

    inventory = inventory_handoff_tree(
        source, EffectProjectionContext(contracts=(source, child))
    )

    assert inventory.cancelled == (child,)
    assert inventory.repairs == ()


def test_typed_terminal_intermediate_hold_does_not_block_live_grandchild():
    source = _contract("source", outputs=("final",))
    intermediate = _contract("intermediate", parent_id=source.id)
    grandchild = _contract("grandchild", parent_id=intermediate.id)
    receipt = create_runtime_record("candidate.received", {"candidate_id": "c"})
    hold = create_runtime_record(
        "claim.hold.placed",
        {
            "contract_id": intermediate.id,
            "reason": "historical hold",
            "actor_id": "human:operator",
            "key_id": "key",
            "candidate_id": "candidate",
        },
        causal_parents=(receipt.id,),
    )
    terminal = create_terminal_record(intermediate.id, "complete")
    inventory = inventory_handoff_tree(
        source,
        EffectProjectionContext(
            contracts=(source, intermediate, grandchild),
            runtime_records=(receipt, hold, terminal),
        ),
    )

    assert inventory.cancelled == (grandchild,)
    assert inventory.repairs == ()


def test_system_observation_repairs_own_output_but_not_business_source_output():
    observation_name = "_tool_obs_result"
    source = _contract("source", outputs=(observation_name,))
    system_child = _contract(
        "system-child",
        parent_id=source.id,
        outputs=(observation_name,),
        origin="system",
    )
    observation = Asset(
        id="asset:tool-observation",
        name=observation_name,
        content="observed",
        created_by=system_child.id,
        origin="tool",
    )
    context = EffectProjectionContext(
        contracts=(source, system_child), assets=(observation,)
    )

    inventory = inventory_handoff_tree(source, context)

    assert inventory.repairs == (system_child,)
    assert inventory.repair_outputs == (
        (system_child.id, ((observation_name, (observation.id,), ()),)),
    )
    assert remaining_output_slots(source, context) == (observation_name,)


def test_human_expanded_handoff_shows_copyable_exact_decisions():
    rendered = _human_handoff(
        {
            "record_id": "record:link",
            "contract_id": "task:source",
            "successor_id": "task:successor-full-id",
            "budget": 4,
            "reason": "continue work",
            "source_original_balance": 0,
            "returned_allowance": 4,
            "return_record_ids": ("record:return-full-id",),
            "allowance_returns": (
                {
                    "donor_contract_id": "task:donor-full-id",
                    "amount": 4,
                    "record_id": "record:return-full-id",
                },
            ),
            "cancelled_contract_ids": ("task:donor-full-id",),
            "repair_contract_ids": (),
            "repair_output_proofs": {},
            "preserved_contract_ids": ("task:verifier-full-id",),
            "run_command": "aig run --task task:successor-full-id",
            "hold_id": "",
        }
    )

    assert "task:donor-full-id" in rendered
    assert "record:return-full-id" in rendered
    assert "task:verifier-full-id" in rendered
    assert "aig run --task task:successor-full-id" in rendered


def _published_tree(
    tmp_path,
    *,
    child_outputs: tuple[str, ...] = (),
    source_inputs: tuple[str, ...] = (),
    include_descendants: bool = True,
):
    store = SQLiteStore(tmp_path / "expanded-handoff.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        (
            "contract.cancel",
            "contract.publish",
            "task.handoff",
            "task.block",
        ),
    )
    revoker = ActorKey(
        "human:revoker",
        "revoker-key",
        signer.kind,
        signer.signer_id,
        ("actor.revoke",),
    )
    genesis = create_genesis_manifest(
        "expanded-handoff", (actor, revoker), "policy:test"
    )
    initialize_genesis(store, genesis)
    source = contract_from_fields(
        name="expanded-source",
        description="root obligation",
        inputs=source_inputs,
        outputs=("final",),
        activation="",
        budget=5,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    assert publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="expanded-source",
    ).accepted

    def declare(
        name: str,
        parent: Contract,
        budget: int,
        outputs: tuple[str, ...] = (),
    ) -> Contract:
        contract = contract_from_fields(
            name=name,
            description=name,
            parent_id=parent.id,
            inputs=(),
            outputs=outputs,
            activation="",
            budget=budget,
            tool_scope=(),
            labels=(),
            origin="human",
        )
        assert publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            contract_declaration_effect(contract),
            idempotency_key=f"declare:{name}",
        ).accepted
        return contract

    child = (
        declare("expanded-child", source, 5, child_outputs)
        if include_descendants
        else None
    )
    grandchild = declare("expanded-grandchild", child, 2) if child else None
    great_grandchild = (
        declare("expanded-great-grandchild", grandchild, 1) if grandchild else None
    )
    return (
        store,
        signer,
        actor,
        genesis,
        source,
        child,
        grandchild,
        great_grandchild,
    )


def test_expanded_handoff_returns_live_tree_balance_and_replays_same_facts(tmp_path):
    from aigineering.core.causal_allowance import allowance_available

    (
        store,
        signer,
        actor,
        genesis,
        source,
        child,
        grandchild,
        great_grandchild,
    ) = _published_tree(tmp_path)
    assert (
        allowance_available(
            source, (record for _, record in store.scan_runtime_records())
        )
        == 0
    )
    assert (
        allowance_available(
            child, (record for _, record in store.scan_runtime_records())
        )
        == 3
    )
    assert (
        allowance_available(
            grandchild, (record for _, record in store.scan_runtime_records())
        )
        == 1
    )
    assert (
        allowance_available(
            great_grandchild, (record for _, record in store.scan_runtime_records())
        )
        == 1
    )
    assert store.claim_contract(grandchild.id, "worker:expanded-active") is not None

    def handoff():
        return publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            task_handoff_effect(source.id, "Continue the root obligation"),
            idempotency_key="expanded-return",
            additional_effects=HANDOFF_EFFECTS,
        )

    accepted = handoff()
    assert accepted.accepted
    link = project_task_handoffs(
        accepted.runtime_records, source_contract_id=source.id
    )[0]
    payload = link.payload
    assert payload["source_original_balance"] == 0
    assert payload["returned_allowance"] == 5
    assert payload["budget"] == 5
    assert set(payload["cancelled_contract_ids"]) == {
        child.id,
        grandchild.id,
        great_grandchild.id,
    }
    return_records = [
        record
        for _, record in store.scan_runtime_records(record_type="allowance.returned")
    ]
    assert {record.payload["from_contract_id"] for record in return_records} == {
        child.id,
        grandchild.id,
        great_grandchild.id,
    }
    assert set(payload["return_record_ids"]) == {record.id for record in return_records}
    assert all(record.id in link.causal_parents for record in return_records)
    successor = store.get_contract(payload["successor_id"])
    assert successor is not None
    assert successor.parent_id == source.id
    assert successor.budget == 5
    assert store.get_claim(grandchild.id)["status"] != "active"

    replay = handoff()
    assert replay.accepted, [
        record.payload
        for record in replay.runtime_records
        if record.record_type == "candidate.rejected"
    ]
    replay_link = project_task_handoffs(
        replay.runtime_records, source_contract_id=source.id
    )[0]
    assert replay_link.id == link.id
    assert replay_link.payload == payload
    assert len(store.scan_runtime_records(record_type="task.handed_off")) == 1
    assert len(store.scan_runtime_records(record_type="allowance.returned")) == 3
    store.close()


def test_expanded_handoff_requires_and_freezes_each_declared_input_role(tmp_path):
    from aigineering.core.input_bindings import read_contract_input_assets

    store, signer, actor, genesis, source, *_descendants = _published_tree(
        tmp_path, source_inputs=("source_material",)
    )

    def handoff(key):
        return publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            task_handoff_effect(source.id, "Continue after freezing inputs"),
            idempotency_key=key,
            additional_effects=HANDOFF_EFFECTS,
        )

    missing = handoff("missing-expanded-input")
    assert not missing.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and "source_material" in record.payload.get("reason", "")
        for record in missing.runtime_records
    )
    assert store.scan_runtime_records(record_type="task.handed_off") == []
    assert store.scan_runtime_records(record_type="allowance.returned") == []
    assert store.scan_runtime_records(record_type="lifecycle.terminal") == []
    assert (
        len(
            [
                contract
                for contract in store.get_all_contracts()
                if contract.parent_id == source.id
            ]
        )
        == 1
    )

    original = sign_asset(
        Asset(
            id="asset:source-material",
            name="source_material",
            content="A source input available before handoff.",
            origin="human",
        ),
        signed_by="human",
    )
    store.add_asset(original)
    accepted = handoff("expanded-input-present")
    assert accepted.accepted, accepted
    link = project_task_handoffs(
        accepted.runtime_records, source_contract_id=source.id
    )[0]
    successor = store.get_contract(link.payload["successor_id"])
    assert successor is not None
    assert successor.id.startswith("task:v6:")
    assert successor.sensitive_input_policy["input_asset_bindings"] == {
        "source_material": (original.id,)
    }

    future = sign_asset(
        Asset(
            id="asset:future-source-material",
            name="source_material",
            content="A later same-name asset must not widen the frozen input.",
            origin="human",
        ),
        signed_by="human",
    )
    store.add_asset(future)
    selected = read_contract_input_assets(successor, store)
    assert [asset.id for asset in selected] == [original.id]
    store.close()


def test_leaf_handoff_keeps_historical_missing_input_behavior(tmp_path):
    store, signer, actor, genesis, source, *_descendants = _published_tree(
        tmp_path,
        source_inputs=("missing_source_material",),
        include_descendants=False,
    )
    result = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(source.id, "Keep legacy leaf behavior"),
        idempotency_key="leaf-missing-input",
        additional_effects=HANDOFF_EFFECTS,
    )

    assert result.accepted, result
    link = project_task_handoffs(result.runtime_records, source_contract_id=source.id)[
        0
    ]
    successor = store.get_contract(link.payload["successor_id"])
    assert successor is not None
    assert successor.inputs == source.inputs
    assert successor.id.startswith("task:v3:")
    store.close()


def _dependency_fact(target_id: str, prerequisite_id: str, key: str):
    effect = task_dependency_link_effect(target_id, prerequisite_id, "ordered work")
    return create_runtime_record(
        "claim.constraint.placed",
        {
            "actor_id": "human:operator",
            "candidate_id": f"candidate:{key}",
            "contract_id": target_id,
            "key_id": "operator-key",
            "predicate": "prerequisite_terminal",
            "prerequisite_contract_id": prerequisite_id,
            "reason": str(effect.payload["reason"]),
            "required_terminal": "complete",
        },
        causal_parents=(f"record:receipt:{key}",),
    )


def _handoff_candidate(source, signer, key):
    effect = task_handoff_effect(source.id, "Continue after dependency review")
    return effect, create_candidate_proposal(
        domain_id="genesis:dependency-handoff-test",
        actor_id="human:operator",
        key_id="operator-key",
        effects=(effect,),
        signer=signer,
        idempotency_key=key,
    )


def test_handoff_blocks_cancelled_target_with_unfinished_external_prerequisite():
    source = _contract("dep-handoff-source", outputs=("final",))
    prerequisite = _contract("dep-handoff-external-prerequisite", outputs=("done",))
    edge = _dependency_fact(source.id, prerequisite.id, "external-in")
    signer = Ed25519Signer()
    effect, candidate = _handoff_candidate(source, signer, "external-in")

    with pytest.raises(ValueError, match=edge.id):
        project_task_handoff_effect(
            effect,
            candidate,
            "record:handoff-receipt",
            EffectProjectionContext(
                contracts=(source, prerequisite), runtime_records=(edge,)
            ),
        )


def test_handoff_preserves_internal_cancelled_edge_as_history_without_transfer(
    tmp_path,
):
    store, _signer, _actor, _genesis, source, child, *_rest = _published_tree(tmp_path)
    edge = _dependency_fact(source.id, child.id, "internal-edge")
    signer = Ed25519Signer()
    effect, candidate = _handoff_candidate(source, signer, "internal-edge")
    context = load_effect_projection_context(store)
    context = EffectProjectionContext(
        contracts=context.contracts,
        assets=context.assets,
        runtime_records=(*context.runtime_records, edge),
        actor_keys=context.actor_keys,
    )

    projection = project_task_handoff_effect(
        effect,
        candidate,
        "record:handoff-receipt",
        context,
    )
    link = next(
        item for item in projection.records if item.record_type == "task.handed_off"
    )
    assert child.id in link.payload["cancelled_contract_ids"]
    assert projection.contract is not None
    assert projection.contract.id != source.id
    assert edge.id not in {item.id for item in projection.records}
    store.close()


def test_handoff_does_not_rewrite_external_downstream_dependency(tmp_path):
    store, signer, _actor, _genesis, source, *_rest = _published_tree(
        tmp_path, include_descendants=False
    )
    downstream = _contract("dep-downstream-target", outputs=("next",))
    edge = _dependency_fact(downstream.id, source.id, "external-out")
    signer = Ed25519Signer()
    effect, candidate = _handoff_candidate(source, signer, "external-out")
    original = load_effect_projection_context(store)
    context = EffectProjectionContext(
        contracts=(*original.contracts, downstream),
        assets=original.assets,
        runtime_records=(*original.runtime_records, edge),
        actor_keys=original.actor_keys,
    )

    projection = project_task_handoff_effect(
        effect,
        candidate,
        "record:handoff-receipt",
        context,
    )
    assert projection.contract is not None
    assert all(item.id != edge.id for item in projection.records)
    store.close()
