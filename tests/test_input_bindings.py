"""Exact input Asset bindings remain part of signed Contract identity."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.disclosure import DisclosurePolicyError, compute_disclosure
from aigineering.core.asset_versions import create_replacement_claim, create_slice_asset
from aigineering.core.domain import initialize_genesis
from aigineering.core.input_bindings import (
    input_asset_bindings,
    read_contract_input_assets,
)
from aigineering.core.provenance import sign_asset
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_migrations import CURRENT_SCHEMA_VERSION
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.verification import _collect_input_assets
from aigineering.plugins.task_semantics import continuation_contract, method_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.identity import (
    contract_from_fields,
    hash_contract_v5,
    validate_contract_identity,
)
from aigineering.protocol.types import Asset


def _contract(policy):
    return contract_from_fields(
        name="pinned source task",
        description="Use the exact input selected at declaration.",
        inputs=("dataset",),
        outputs=("result",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        sensitive_input_policy=policy,
        origin="human",
    )


def test_input_bindings_use_exact_ids_before_and_after_same_name_publication(
    tmp_path,
):
    store = SQLiteStore(tmp_path / "bindings.sqlite")
    signer = Ed25519Signer()
    operator = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish", "contract.publish"),
    )
    genesis = create_genesis_manifest("input-bindings-test", (operator,), "policy:test")
    initialize_genesis(store, genesis)
    original_result = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(Asset(id="unused", name="dataset", content="chosen")),
        idempotency_key="publish-chosen-dataset",
    )
    assert original_result.accepted
    original = original_result.assets[0]
    contract = _contract({"input_asset_bindings": {"dataset": [original.id]}})
    declaration = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="declare-pinned-input-contract",
    )
    assert declaration.accepted, declaration

    later_twin_result = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(
            Asset(id="unused-twin", name="dataset", content="later twin")
        ),
        idempotency_key="publish-later-same-name-dataset",
    )
    assert later_twin_result.accepted
    reopened = SQLiteStore(store._db_path)
    try:
        persisted = reopened.get_contract(contract.id)
        assert persisted is not None and persisted.id.startswith("task:v6:")
        assert [
            asset.id for asset in read_contract_input_assets(persisted, reopened)
        ] == [original.id]
        assert {asset.id for asset in compute_disclosure(persisted, reopened)} == {
            original.id
        }
        assert [asset.id for asset in _collect_input_assets(persisted, reopened)] == [
            original.id
        ]
    finally:
        reopened.close()
        store.close()


def test_invalid_active_bindings_fail_closed_and_unused_roles_are_inert():
    original = sign_asset(
        Asset(id="asset:original", name="dataset", content="chosen"), "test"
    )
    twin = sign_asset(Asset(id="asset:twin", name="dataset", content="twin"), "test")
    store = MemoryStore()
    store.add_asset(original)
    store.add_asset(twin)

    pinned = _contract(
        {"input_asset_bindings": {"dataset": [original.id], "unused": ["missing"]}}
    )
    assert input_asset_bindings(pinned.sensitive_input_policy)["dataset"] == (
        original.id,
    )
    assert read_contract_input_assets(pinned, store) == [original]
    validate_contract_commitment(pinned, assets=(original, twin))

    empty_pin = _contract({"input_asset_bindings": {"dataset": []}})
    assert read_contract_input_assets(empty_pin, store) == []

    with pytest.raises(ValueError, match="missing Asset"):
        validate_contract_commitment(
            _contract({"input_asset_bindings": {"dataset": ["missing"]}}),
            assets=(original, twin),
        )
    with pytest.raises(ValueError, match="does not match its role"):
        validate_contract_commitment(
            _contract({"input_asset_bindings": {"dataset": ["asset:other"]}}),
            assets=(Asset(id="asset:other", name="other", content="x"),),
        )
    with pytest.raises(ValueError, match="tombstoned"):
        validate_contract_commitment(
            _contract({"input_asset_bindings": {"dataset": ["asset:tombstone"]}}),
            assets=(replace(original, id="asset:tombstone", tombstoned=True),),
        )


def test_bound_inputs_keep_signer_and_trust_policy_and_do_not_disclose_redacted_assets():
    selected = sign_asset(
        Asset(
            id="asset:selected",
            name="dataset",
            content="approved source",
            trust_tier="observed",
        ),
        "trusted-source",
    )
    twin = sign_asset(
        Asset(
            id="asset:twin",
            name="dataset",
            content="different source",
            trust_tier="observed",
        ),
        "other-source",
    )
    redacted = sign_asset(
        Asset(
            id="asset:redacted",
            name="hidden",
            content="must remain redacted",
            disclosure_view="redacted",
        ),
        "trusted-source",
    )
    store = MemoryStore()
    for asset in (selected, twin, redacted):
        store.add_asset(asset)

    pinned = contract_from_fields(
        name="trusted pinned input",
        description="Use the exact trusted source.",
        inputs=("dataset",),
        outputs=("result",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=("hidden",),
        origin="human",
        sensitive_input_policy={
            "required_signer": "trusted-source",
            "required_trust_tier": "observed",
            "input_asset_bindings": {"dataset": [selected.id]},
        },
    )
    disclosed = compute_disclosure(pinned, store)
    by_id = {asset.id: asset for asset in disclosed}
    assert selected.id in by_id
    assert twin.id not in by_id
    assert redacted.id not in by_id

    legacy_name_selection = contract_from_fields(
        name="unpinned trusted input",
        description="The old role selects all matching names.",
        inputs=("dataset",),
        outputs=("result",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="human",
        sensitive_input_policy={
            "required_signer": "trusted-source",
            "required_trust_tier": "observed",
        },
    )
    with pytest.raises(DisclosurePolicyError, match="signer"):
        compute_disclosure(legacy_name_selection, store)


def test_exact_slice_binding_preserves_verified_claim_policy_despite_name_twin():
    source_content = "row 1\nrow 2\n"
    source = sign_asset(
        Asset(
            id="asset:source",
            name="source-data",
            content=source_content,
            definition_hash=hash_asset_definition("source-data"),
            content_hash=hash_asset_content("source-data", source_content),
            origin="tool",
            trust_tier="observed",
        ),
        "source-signer",
    )
    selected = create_slice_asset(source, slice_name="dataset", range_spec="lines:1-1")
    claim = create_replacement_claim(
        source.id,
        selected.id,
        definition_hash=selected.definition_hash,
        claim_type="slice",
        lineage_id=source.id,
        range_spec="lines:1-1",
    )
    twin = sign_asset(
        Asset(
            id="asset:unclaimed-twin",
            name="dataset",
            content="unverified twin",
            origin="worker",
        ),
        "worker-signer",
    )
    store = MemoryStore()
    for asset in (source, selected, twin):
        store.add_asset(asset)
    store.add_replacement_claim(claim)

    pinned = contract_from_fields(
        name="verified slice input",
        description="Use the exact derived slice.",
        inputs=("dataset",),
        outputs=("result",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="human",
        sensitive_input_policy={
            "accepted_claim_types": ["slice"],
            "input_asset_bindings": {"dataset": [selected.id]},
        },
    )
    assert compute_disclosure(pinned, store) == [selected]

    unpinned = contract_from_fields(
        name="name-selected inputs",
        description="A same-name twin must still fail the slice rule.",
        inputs=("dataset",),
        outputs=("result",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="human",
        sensitive_input_policy={"accepted_claim_types": ["slice"]},
    )
    with pytest.raises(DisclosurePolicyError, match="no verified incoming claim"):
        compute_disclosure(unpinned, store)


@pytest.mark.parametrize(
    "bindings",
    [
        [],
        {"dataset": "asset:id"},
        {"dataset": ["asset:z", "asset:a"]},
        {"dataset": ["asset:a", "asset:a"]},
        {"": ["asset:a"]},
        {"dataset": [""]},
    ],
)
def test_input_binding_schema_is_strict(bindings):
    with pytest.raises(ValueError, match="input_asset_bindings"):
        input_asset_bindings({"input_asset_bindings": bindings})


def test_input_binding_marker_requires_v6_and_binds_all_contract_fields():
    policy = {"input_asset_bindings": {"dataset": ["asset:one"]}}
    v6 = _contract(policy)
    assert v6.id.startswith("task:v6:")
    validate_contract_identity(v6)
    tampered = replace(
        v6,
        sensitive_input_policy={"input_asset_bindings": {"dataset": ["asset:two"]}},
    )
    with pytest.raises(ValueError, match="canonical v6"):
        validate_contract_identity(tampered)

    legacy_id = hash_contract_v5(
        name=v6.name,
        description=v6.description,
        inputs=v6.inputs,
        outputs=v6.outputs,
        activation=v6.activation,
        budget=v6.budget,
        tool_scope=v6.tool_scope,
        labels=v6.labels,
        origin=v6.origin,
        parent_id=v6.parent_id,
        worker_capabilities=v6.worker_capabilities,
        worker_pools=v6.worker_pools,
        delegation_capabilities=v6.delegation_capabilities,
        delegation_pools=v6.delegation_pools,
        minting_authority=v6.minting_authority,
        sensitive_input_policy=v6.sensitive_input_policy,
        acceptance_policy=v6.acceptance_policy,
        context_asset_ids=v6.context_asset_ids,
    )
    legacy = replace(v6, id=legacy_id)
    with pytest.raises(ValueError, match="require a task:v6 identity"):
        validate_contract_identity(legacy)


def test_tool_continuation_carries_binding_identity_without_widening_inputs():
    parent = contract_from_fields(
        name="tool parent",
        description="Use the fixed dataset.",
        inputs=("dataset",),
        outputs=("result",),
        activation="",
        budget=5,
        tool_scope=("lookup",),
        labels=(),
        origin="human",
        sensitive_input_policy={"input_asset_bindings": {"dataset": ["asset:chosen"]}},
    )
    tool = method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    continuation = continuation_contract(parent, tool, method="tool", budget=2)
    assert continuation.id.startswith("task:v6:")
    assert continuation.sensitive_input_policy["input_asset_bindings"] == {
        "dataset": ("asset:chosen",)
    }
    assert continuation.inputs == ()
    validate_contract_identity(continuation)


def test_schema_18_marker_preserves_schema_17_facts_and_materializations(tmp_path):
    path = tmp_path / "schema-17.sqlite"
    store = SQLiteStore(path)
    signer = Ed25519Signer()
    operator = ActorKey(
        "human:migration",
        "migration-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish",),
    )
    genesis = create_genesis_manifest("schema-marker-test", (operator,), "p:test")
    initialize_genesis(store, genesis)
    committed = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(Asset(id="unused", name="kept", content="unchanged")),
        idempotency_key="schema-17-preserve-fact",
    )
    assert committed.accepted
    before_ids = tuple(record.id for _, record in store.scan_runtime_records())
    before_digest = store.runtime_materialization_digest()
    before_tables = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    store._conn.execute("UPDATE schema_version SET version = 17")
    store._conn.commit()
    store.close()

    reopened = SQLiteStore(path)
    try:
        assert reopened.schema_version == CURRENT_SCHEMA_VERSION == 21
        assert (
            tuple(record.id for _, record in reopened.scan_runtime_records())
            == before_ids
        )
        assert reopened.runtime_materialization_digest() == before_digest
        after_tables = {
            row[0]
            for row in reopened._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert after_tables == before_tables
    finally:
        reopened.close()
