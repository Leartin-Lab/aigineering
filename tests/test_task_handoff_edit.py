"""Pure task-edit handoff projection boundaries."""

from __future__ import annotations

import pytest

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.claim_assignments import (
    assignment_context_from_projection,
    effective_assignment,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.projection_context import (
    EffectProjectionContext,
    load_effect_projection_context,
)
from aigineering.core.claim_holds import open_claim_holds
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.handoff import (
    HANDOFF_EFFECTS,
    project_task_handoff_effect,
    validate_task_edit_request,
    validate_task_edit_snapshot,
)
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
from aigineering.plugins.task_assignment import TASK_ASSIGNMENT_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    candidate_received_record,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_revocation_effect,
    contract_declaration_effect,
    task_assign_effect,
    task_block_effect,
    task_handoff_effect,
)
from aigineering.protocol.identity import compute_content_hash
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.wire import contract_to_dict


_CAPABILITIES = (
    "contract.cancel",
    "contract.publish",
    "task.handoff",
    "task.edit",
    "task.block",
)


def _fixture(
    *,
    holds: tuple[str, ...] = (),
    title="new title",
    body="new body",
    source_contract=None,
    budget=None,
):
    signer = Ed25519Signer.from_private_key_hex("11" * 32)
    actor = ActorKey(
        "human:edit",
        "edit-key",
        signer.kind,
        signer.signer_id,
        _CAPABILITIES,
    )
    genesis = create_genesis_manifest("task-edit-test", (actor,), "policy:test")
    source = source_contract or build_control_plane_contract(
        name="old title", description="old body", outputs=("result",), budget=5
    )
    declaration = create_runtime_record(
        "contract.declared", {"contract": contract_to_dict(source)}, causal_parents=()
    )
    source_holds = tuple(
        create_runtime_record(
            "claim.hold.placed",
            {
                "actor_id": "human:seed",
                "candidate_id": f"candidate:seed:{index}",
                "contract_id": source.id,
                "key_id": "seed-key",
                "reason": reason,
            },
            causal_parents=(f"record:seed-receipt-{index}",),
        )
        for index, reason in enumerate(holds)
    )
    hold_ids = tuple(sorted(record.id for record in source_holds))
    edit = {"source_assignment_id": "", "source_hold_ids": hold_ids}
    if title is not None:
        edit["title"] = title
    if body is not None:
        edit["body"] = body
    effect = task_handoff_effect(
        source.id, "revise exact task", budget=budget, edit=edit
    )
    parents = (source.id, declaration.id, *hold_ids)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(effect,),
        signer=signer,
        causal_parents=parents,
        idempotency_key="task-edit-test-key",
    )
    receipt = candidate_received_record(candidate, genesis)
    context = EffectProjectionContext(
        contracts=(source,),
        runtime_records=(declaration, *source_holds),
        actor_keys=(actor,),
    )
    projection = project_task_handoff_effect(effect, candidate, receipt.id, context)
    return (
        signer,
        actor,
        genesis,
        source,
        declaration,
        source_holds,
        edit,
        effect,
        candidate,
        receipt,
        context,
        projection,
    )


def test_edit_intent_replaces_exact_fields_and_copies_all_source_holds():
    (
        *_head,
        source,
        _declaration,
        source_holds,
        _edit,
        _effect,
        candidate,
        receipt,
        _ctx,
        projection,
    ) = _fixture(
        holds=("one", "two", "three"),
        title="reviewed title",
        body="",
    )
    successor = projection.contract
    assert successor is not None
    assert successor.name == "reviewed title"
    assert successor.description == ""
    assert successor.parent_id == source.id
    assert successor.outputs == source.outputs
    assert successor.budget == source.budget

    link = next(
        record
        for record in projection.records
        if record.record_type == "task.handed_off"
    )
    assert link.payload["edit"]["changed_fields"] == ("title", "body")
    mapping = link.payload["edit"]["hold_mapping"]
    assert tuple(item["source_hold_id"] for item in mapping) == tuple(
        record.id for record in sorted(source_holds, key=lambda record: record.id)
    )
    copied_ids_in_source_order = tuple(item["successor_hold_id"] for item in mapping)
    assert copied_ids_in_source_order != tuple(sorted(copied_ids_in_source_order))
    copied = tuple(
        record
        for record in projection.records
        if record.record_type == "claim.hold.placed"
    )
    assert len(copied) == len(source_holds)
    assert {record.payload["reason"] for record in copied} == {
        record.payload["reason"] for record in source_holds
    }
    assert all(receipt.id in record.causal_parents for record in copied)
    assert "task.edit" in projection.additional_capabilities
    assert "task.block" in projection.additional_capabilities
    assert candidate.causal_parents == (
        source.id,
        _declaration.id,
        *tuple(sorted(record.id for record in source_holds)),
    )


@pytest.mark.parametrize(
    ("edit_fields", "expected_name", "expected_description", "changed_fields"),
    [
        ({"title": "Revised name"}, "Revised name", "Original brief", ("title",)),
        ({"body": ""}, "Original name", "", ("body",)),
        (
            {"title": "Revised name", "body": "Revised brief"},
            "Revised name",
            "Revised brief",
            ("title", "body"),
        ),
    ],
)
def test_title_and_body_edit_preserve_every_other_contract_field(
    edit_fields, expected_name, expected_description, changed_fields
):
    acceptance_policy = {
        "mode": "independent",
        "policy_version": "field-preservation-v1",
        "rubric_asset_ids": [],
        "evidence_asset_ids": [],
        "required_attestations": 1,
        "verifier_capabilities": ["review"],
    }
    source = contract_from_fields(
        name="Original name",
        description="Original brief",
        parent_id=None,
        inputs=(),
        outputs=("report", "appendix"),
        activation="manual:review-approved",
        budget=9,
        tool_scope=("filesystem.read", "python.execute"),
        labels=("discipline:analysis", "skill:review"),
        context_asset_ids=(),
        worker_capabilities=("analysis", "citation-check"),
        worker_pools=("researchers",),
        delegation_capabilities=("analysis.delegate",),
        delegation_pools=("bounded-research",),
        origin="human",
        minting_authority=(),
        sensitive_input_policy={"input_asset_bindings": {}},
        acceptance_policy=acceptance_policy,
    )
    values = _fixture(
        title=edit_fields.get("title"),
        body=edit_fields.get("body"),
        source_contract=source,
        budget=4,
    )
    projected_source = values[3]
    projection = values[-1]
    successor = projection.contract
    assert successor is not None

    original_fields = contract_to_dict(projected_source)
    successor_fields = contract_to_dict(successor)
    allowed_changes = {"id", "parent_id", "name", "description", "outputs", "budget"}
    assert original_fields.keys() == successor_fields.keys()
    assert {
        field: value
        for field, value in successor_fields.items()
        if field not in allowed_changes
    } == {
        field: value
        for field, value in original_fields.items()
        if field not in allowed_changes
    }
    assert successor.id != projected_source.id
    assert successor.parent_id == projected_source.id
    assert successor.name == expected_name
    assert successor.description == expected_description
    assert successor.outputs == projected_source.outputs
    assert successor.budget == 4

    link = next(
        record
        for record in projection.records
        if record.record_type == "task.handed_off"
    )
    assert link.payload["edit"]["changed_fields"] == changed_fields


def test_edit_replay_survives_release_of_original_source_hold():
    (
        _signer,
        _actor,
        genesis,
        source,
        declaration,
        source_holds,
        _edit,
        effect,
        candidate,
        receipt,
        context,
        projection,
    ) = _fixture(holds=("waiting", "checking"))
    committed_ids = tuple(record.id for record in projection.records)
    committed = create_runtime_record(
        "candidate.committed",
        {"candidate_id": candidate.id, "committed_record_ids": committed_ids},
        causal_parents=(receipt.id, *committed_ids),
    )
    releases = tuple(
        create_runtime_record(
            "claim.hold.released",
            {
                "actor_id": "human:release",
                "candidate_id": f"candidate:release:{index}",
                "contract_id": source.id,
                "hold_id": hold.id,
                "key_id": "release-key",
                "reason": "source is cancelled",
            },
            causal_parents=(f"record:release-receipt-{index}", hold.id),
        )
        for index, hold in enumerate(source_holds)
    )
    replay_context = EffectProjectionContext(
        contracts=(source, projection.contract),
        runtime_records=(
            declaration,
            *source_holds,
            receipt,
            *projection.records,
            committed,
            *releases,
        ),
        actor_keys=context.actor_keys,
    )
    assert not open_claim_holds(replay_context.runtime_records, contract_id=source.id)
    replay = project_task_handoff_effect(effect, candidate, receipt.id, replay_context)
    assert tuple(record.id for record in replay.records) == committed_ids
    assert replay.contract == projection.contract


@pytest.mark.parametrize(
    "source,title,body",
    [
        ("é", "e\u0301", "old body"),
        ("old title", "old title", "e\u0301"),
    ],
)
def test_canonical_unicode_equivalent_text_is_a_noop(source, title, body):
    contract = build_control_plane_contract(
        name=source,
        description="é" if body == "e\u0301" else "old body",
        outputs=("result",),
        budget=1,
    )
    with pytest.raises(ValueError, match="no-op"):
        validate_task_edit_request(
            {
                "source_assignment_id": "",
                "source_hold_ids": (),
                "title": title,
                "body": body,
            },
            contract,
        )


def test_edit_request_snapshot_requires_exact_sorted_hold_set():
    _, _, _, source, _, _, edit, *_ = _fixture(holds=("hold",))
    intent = validate_task_edit_request(edit, source)
    validate_task_edit_snapshot(
        intent,
        source_assignment_id="",
        source_hold_ids=edit["source_hold_ids"],
    )
    with pytest.raises(ValueError, match="hold set changed"):
        validate_task_edit_snapshot(intent, source_assignment_id="", source_hold_ids=())
    assert compute_content_hash("é") == compute_content_hash("e\u0301")


def test_edit_shape_rejects_null_unknown_unsorted_duplicate_and_over_limit_holds():
    source = build_control_plane_contract(
        name="before", description="description", outputs=("result",), budget=1
    )
    base = {"source_assignment_id": "", "source_hold_ids": (), "title": "after"}
    for edit in (
        None,
        {**base, "unexpected": True},
        {**base, "source_hold_ids": ("z", "a")},
        {**base, "source_hold_ids": ("same", "same")},
        {**base, "source_hold_ids": tuple(f"hold:{i:02d}" for i in range(65))},
    ):
        with pytest.raises(ValueError):
            validate_task_edit_request(edit, source)
    accepted = validate_task_edit_request(
        {**base, "source_hold_ids": tuple(f"hold:{i:02d}" for i in range(64))},
        source,
    )
    assert len(accepted.source_hold_ids) == 64


def test_edit_projection_copies_the_full_sixty_four_hold_boundary():
    values = _fixture(holds=tuple(f"hold reason {index}" for index in range(64)))
    projection = values[-1]
    copied = tuple(
        record
        for record in projection.records
        if record.record_type == "claim.hold.placed"
    )
    link = next(
        record
        for record in projection.records
        if record.record_type == "task.handed_off"
    )
    assert len(copied) == 64
    assert len(link.payload["edit"]["hold_mapping"]) == 64


def test_signed_store_commit_accepts_exactly_sixty_four_copied_holds(tmp_path):
    store = SQLiteStore(tmp_path / "handoff-edit-64.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:edit-boundary",
        "edit-boundary-key",
        signer.kind,
        signer.signer_id,
        _CAPABILITIES,
    )
    genesis = create_genesis_manifest(
        "task-edit-hold-boundary", (actor,), "policy:test"
    )
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="hold-boundary", outputs=("result",), budget=4
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-hold-boundary",
    )
    assert declared.accepted
    for index in range(64):
        placed = publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            task_block_effect(source.id, f"hold reason {index}"),
            idempotency_key=f"hold-boundary-{index}",
            additional_effects=COLLABORATION_EFFECTS,
        )
        assert placed.accepted
    records = tuple(record for _, record in store.scan_runtime_records())
    source_declaration = next(
        record
        for record in records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == source.id
    )
    hold_ids = tuple(
        sorted(record.id for record in open_claim_holds(records, contract_id=source.id))
    )
    decision = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(
            source.id,
            "edit exact task at hold boundary",
            edit={
                "source_assignment_id": "",
                "source_hold_ids": hold_ids,
                "title": "new hold-boundary title",
            },
        ),
        idempotency_key="edit-hold-boundary",
        causal_parents=(source.id, source_declaration.id, *hold_ids),
        additional_effects=HANDOFF_EFFECTS,
    )
    assert decision.accepted
    links = [
        record
        for _, record in store.scan_runtime_records(record_type="task.handed_off")
        if record.payload.get("contract_id") == source.id
    ]
    assert len(links) == 1
    copied = [
        record
        for _, record in store.scan_runtime_records(record_type="claim.hold.placed")
        if record.payload.get("contract_id") == links[0].payload["successor_id"]
    ]
    assert len(copied) == 64
    assert len(links[0].payload["edit"]["hold_mapping"]) == 64
    store.close()


def test_edit_effect_requires_explicit_task_edit_authority(tmp_path):
    store = SQLiteStore(tmp_path / "handoff-edit-authority.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:no-edit-authority",
        "no-edit-authority-key",
        signer.kind,
        signer.signer_id,
        ("contract.cancel", "contract.publish", "task.handoff"),
    )
    genesis = create_genesis_manifest("task-edit-authority", (actor,), "policy:test")
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="authority source", outputs=("result",), budget=2
    )
    declaration = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-authority-source",
    )
    assert declaration.accepted
    source_declaration = next(
        record
        for _, record in store.scan_runtime_records(record_type="contract.declared")
        if record.payload["contract"]["id"] == source.id
    )
    rejected = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(
            source.id,
            "unauthorized edit attempt",
            edit={
                "source_assignment_id": "",
                "source_hold_ids": (),
                "title": "new title",
            },
        ),
        idempotency_key="edit-without-capability",
        causal_parents=(source.id, source_declaration.id),
        additional_effects=HANDOFF_EFFECTS,
    )
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == rejected.candidate_id
        for record in rejected.runtime_records
    )
    assert store.get_contract(source.id) == source
    assert not list(store.scan_runtime_records(record_type="task.handed_off"))
    assert not any(contract.id != source.id for contract in store.get_all_contracts())
    store.close()


def test_edit_with_copied_holds_requires_task_block_authority(tmp_path):
    store = SQLiteStore(tmp_path / "handoff-edit-block-authority.sqlite")
    signer = Ed25519Signer()
    hold_signer = Ed25519Signer()
    actor = ActorKey(
        "human:no-block-authority",
        "no-block-authority-key",
        signer.kind,
        signer.signer_id,
        ("contract.cancel", "contract.publish", "task.handoff", "task.edit"),
    )
    hold_actor = ActorKey(
        "human:hold-placer",
        "hold-placer-key",
        hold_signer.kind,
        hold_signer.signer_id,
        ("task.block",),
    )
    genesis = create_genesis_manifest(
        "task-edit-block-authority", (actor, hold_actor), "policy:test"
    )
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="held authority source", outputs=("result",), budget=2
    )
    assert publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-block-authority-source",
    ).accepted
    held = publish_effect(
        store,
        store,
        genesis,
        hold_actor,
        hold_signer,
        task_block_effect(source.id, "must be copied"),
        idempotency_key="place-block-authority-hold",
        additional_effects=COLLABORATION_EFFECTS,
    )
    assert held.accepted
    source_declaration = next(
        record
        for _, record in store.scan_runtime_records(record_type="contract.declared")
        if record.payload["contract"]["id"] == source.id
    )
    source_hold = next(
        record
        for _, record in store.scan_runtime_records(record_type="claim.hold.placed")
        if record.payload["contract_id"] == source.id
    )

    rejected = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(
            source.id,
            "copy hold without authority",
            edit={
                "source_assignment_id": "",
                "source_hold_ids": (source_hold.id,),
                "title": "edited while held",
            },
        ),
        idempotency_key="edit-needs-block-capability",
        causal_parents=(source.id, source_declaration.id, source_hold.id),
        additional_effects=HANDOFF_EFFECTS,
    )
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == rejected.candidate_id
        for record in rejected.runtime_records
    )
    assert store.get_contract(source.id) == source
    assert not list(store.scan_runtime_records(record_type="task.handed_off"))
    assert not any(contract.id != source.id for contract in store.get_all_contracts())
    store.close()


def test_edit_exact_replay_rejects_after_operator_key_revocation(tmp_path):
    store = SQLiteStore(tmp_path / "handoff-edit-revoked-replay.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:revocable-edit",
        "revocable-edit-key",
        signer.kind,
        signer.signer_id,
        (*_CAPABILITIES, "actor.revoke"),
    )
    genesis = create_genesis_manifest(
        "task-edit-revoked-replay", (actor,), "policy:test"
    )
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="revocable edit source", outputs=("result",), budget=2
    )
    assert publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-revocable-edit-source",
    ).accepted
    source_declaration = next(
        record
        for _, record in store.scan_runtime_records(record_type="contract.declared")
        if record.payload["contract"]["id"] == source.id
    )
    edit_effect = task_handoff_effect(
        source.id,
        "publish edit before key revocation",
        edit={
            "source_assignment_id": "",
            "source_hold_ids": (),
            "title": "edited once",
        },
    )
    parents = (source.id, source_declaration.id)
    first = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        edit_effect,
        idempotency_key="revoked-edit-original-key",
        causal_parents=parents,
        additional_effects=HANDOFF_EFFECTS,
    )
    assert first.accepted

    revoked = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        actor_revocation_effect(actor.actor_id, actor.key_id, "retired edit key"),
        idempotency_key="revoke-edit-key",
    )
    assert revoked.accepted

    replay = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        edit_effect,
        idempotency_key="revoked-edit-original-key",
        causal_parents=parents,
        additional_effects=HANDOFF_EFFECTS,
    )
    assert not replay.accepted
    # Current-key authorization fails before a new receipt/rejection fact is
    # written. The original durable edit remains the only link.
    links = list(store.scan_runtime_records(record_type="task.handed_off"))
    assert len(links) == 1
    assert links[0][1].payload["candidate_id"] == first.candidate_id
    store.close()


def test_edit_uses_expanded_handoff_scope_and_repairs_child_without_widening_policy():
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:edit-expanded",
        "edit-expanded-key",
        signer.kind,
        signer.signer_id,
        _CAPABILITIES,
    )
    genesis = create_genesis_manifest("task-edit-expanded", (actor,), "policy:test")
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "rubric_asset_ids": [],
        "evidence_asset_ids": [],
        "required_attestations": 1,
        "verifier_capabilities": ["review"],
    }
    source = build_control_plane_contract(
        name="tree title",
        description="tree body",
        outputs=("final",),
        budget=4,
        acceptance_policy=policy,
    )
    child = contract_from_fields(
        name="completed draft",
        description="child task",
        parent_id=source.id,
        inputs=(),
        outputs=("draft",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    asset = Asset(
        id="asset:edit-repair",
        name="draft",
        content="finished draft",
        created_by=child.id,
        origin="human",
    )
    source_declaration = create_runtime_record(
        "contract.declared", {"contract": contract_to_dict(source)}
    )
    child_asset = create_runtime_record(
        "asset.committed",
        {"asset": {"id": asset.id}, "contract_id": child.id},
    )
    edit = {
        "source_assignment_id": "",
        "source_hold_ids": (),
        "body": "Replacement body",
    }
    effect = task_handoff_effect(source.id, "correct scope", edit=edit)
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(effect,),
        signer=signer,
        causal_parents=(source.id, source_declaration.id),
        idempotency_key="edit-expanded-policy",
    )
    receipt = candidate_received_record(candidate, genesis)
    projection = project_task_handoff_effect(
        effect,
        candidate,
        receipt.id,
        EffectProjectionContext(
            contracts=(source, child),
            assets=(asset,),
            runtime_records=(source_declaration, child_asset),
        ),
    )
    successor = projection.contract
    assert successor is not None
    assert successor.name == source.name
    assert successor.description == "Replacement body"
    assert successor.outputs == ("final",)
    assert successor.acceptance_policy == source.acceptance_policy
    assert successor.parent_id == source.id
    assert successor.id.startswith("task:v6:")
    assert "completed draft" not in successor.name
    link = next(
        record
        for record in projection.records
        if record.record_type == "task.handed_off"
    )
    repair = next(
        record
        for record in projection.records
        if record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == child.id
    )
    assert repair.payload["terminal"] == "complete"
    assert asset.id in repair.causal_parents
    assert link.payload["repair_record_ids"] == (repair.id,)
    assert link.payload["edit"]["changed_fields"] == ("body",)
    assert link.payload["edit"]["successor_declaration_id"] in link.causal_parents


def test_signed_expanded_edit_preserves_verifier_and_inherits_assignment(tmp_path):
    store = SQLiteStore(tmp_path / "handoff-edit-expanded-signed.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:expanded-edit",
        "expanded-edit-key",
        signer.kind,
        signer.signer_id,
        (
            "contract.cancel",
            "contract.publish",
            "task.handoff",
            "task.edit",
            "task.assignment.manage",
        ),
    )
    genesis = create_genesis_manifest(
        "task-edit-expanded-signed", (actor,), "policy:test"
    )
    initialize_genesis(store, genesis)
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "rubric_asset_ids": [],
        "evidence_asset_ids": [],
        "required_attestations": 1,
        "verifier_capabilities": ["review"],
    }
    ancestor = build_control_plane_contract(
        name="outer independent obligation",
        outputs=("final",),
        budget=8,
        acceptance_policy=policy,
    )
    assert publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(ancestor),
        idempotency_key="declare-edit-outer-ancestor",
    ).accepted
    source = contract_from_fields(
        name="signed expanded source",
        description="source brief",
        parent_id=ancestor.id,
        inputs=(),
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
        idempotency_key="declare-signed-expanded-source",
    ).accepted

    def declare_child(name, *, outputs=(), inputs=(), capabilities=(), budget=2):
        child = contract_from_fields(
            name=name,
            description=f"{name} brief",
            parent_id=source.id,
            inputs=inputs,
            outputs=outputs,
            activation="",
            budget=budget,
            tool_scope=(),
            labels=(),
            worker_capabilities=capabilities,
            origin="human",
        )
        decision = publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            contract_declaration_effect(child),
            idempotency_key=f"declare:{name}",
        )
        assert decision.accepted
        return child

    unfinished = declare_child("unfinished draft", outputs=("draft",), budget=2)
    verifier = declare_child(
        "independent verifier",
        inputs=("final",),
        capabilities=("review",),
        budget=1,
    )
    store.register_worker(WorkerRegistration("worker:expanded-editor", capacity=2))
    assigned = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_assign_effect(source.id, "worker:expanded-editor", "", "keep this owner"),
        idempotency_key="assign-expanded-edit-source",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )
    assert assigned.accepted
    source_assignment = next(
        record
        for record in assigned.runtime_records
        if record.record_type == "claim.assignment.changed"
    )
    source_declaration = next(
        record
        for _, record in store.scan_runtime_records(record_type="contract.declared")
        if record.payload["contract"]["id"] == source.id
    )
    edit = {
        "source_assignment_id": source_assignment.id,
        "source_hold_ids": (),
        "title": "edited expanded source",
    }
    result = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(source.id, "edit expanded obligation", edit=edit),
        idempotency_key="edit-expanded-source",
        causal_parents=(source.id, source_declaration.id, source_assignment.id),
        additional_effects=HANDOFF_EFFECTS,
    )
    assert result.accepted
    successor = result.contracts[0]
    link = next(
        record
        for record in result.runtime_records
        if record.record_type == "task.handed_off"
    )
    assert successor.name == "edited expanded source"
    assert successor.description == source.description
    assert successor.outputs == source.outputs
    assert successor.acceptance_policy == source.acceptance_policy
    assert successor.budget == source.budget - verifier.budget
    assert link.payload["assignment_mode"] == "inherit"
    assert link.payload["source_assignment_id"] == source_assignment.id
    assert link.payload["worker_id"] == "worker:expanded-editor"
    assert unfinished.id in link.payload["cancelled_contract_ids"]
    assert verifier.id not in link.payload["cancelled_contract_ids"]
    assert store.get_contract(source.id) == source
    assert store.get_contract(successor.id) == successor
    terminals = {
        record.payload["contract_id"]: record.payload["terminal"]
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    }
    assert terminals[source.id] == "cancelled"
    assert terminals[unfinished.id] == "cancelled"
    assert verifier.id not in terminals
    assert ancestor.id not in terminals
    successor_assignment = effective_assignment(
        assignment_context_from_projection(load_effect_projection_context(store)),
        contract_id=successor.id,
    )
    assert successor_assignment is not None
    assert successor_assignment.payload["worker_id"] == "worker:expanded-editor"
    store.close()
