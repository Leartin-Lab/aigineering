"""Output satisfaction must not let stale repair inputs satisfy repair tasks."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from aigineering.core.contract_admission import validate_acceptance_policy
from aigineering.core.output_satisfaction import (
    NON_OUTPUT_PREFIXES,
    all_outputs_satisfied,
    output_asset_meets_conditions,
    unmet_output_names,
    validate_output_requirements,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract


class _AssetLookup:
    def read_task_facts(self, contract_id, record_types):
        return tuple(
            row
            for _, row in self.scan_runtime_records()
            if row.record_type in record_types
            and row.payload.get("contract_id") == contract_id
        )

    def __init__(self, assets: Sequence[Asset], records=()) -> None:
        self.assets = tuple(assets)
        self.records = tuple(records)

    def get_assets_by_name(self, name: str) -> tuple[Asset, ...]:
        return tuple(asset for asset in self.assets if asset.name == name)

    def get_assets_by_definition(self, definition_hash: str) -> tuple[Asset, ...]:
        return tuple(
            asset for asset in self.assets if asset.definition_hash == definition_hash
        )

    def scan_runtime_records(self, *, after_revision=0, record_type=None):
        return tuple(
            (revision, record)
            for revision, record in enumerate(self.records, start=1)
            if revision > after_revision
            and (record_type is None or record.record_type == record_type)
        )

    def get_contract(self, contract_id: str):
        return None


def test_protected_observation_role_is_not_owned_by_task_origin_or_creator():
    output = "_plan_result_parent"
    stale = Asset(
        id="bad-plan",
        name=output,
        content='{"contracts":[]}',
        created_by="failed-plan",
        origin="llm",
    )
    repair = Contract(
        id="repair-plan",
        name="root.plan.recover",
        outputs=(output,),
        origin="system",
        minting_authority=(output,),
    )

    assert all_outputs_satisfied(repair, _AssetLookup((stale,))) is True

    corrected = Asset(
        id="corrected-plan",
        name=output,
        content='{"contracts":[{"name":"child"}]}',
        created_by=repair.id,
        origin="llm",
    )
    assert all_outputs_satisfied(repair, _AssetLookup((stale, corrected))) is True


@pytest.mark.parametrize(
    "policy",
    [
        None,
        {"mode": "mechanical"},
        {"mode": "mechanical", "output_source": "contract_tree"},
    ],
)
def test_legacy_recovery_and_tree_fields_do_not_add_producer_conditions(policy):
    output = "recovered_output"
    recovery = Contract(
        id="recovery-task",
        name="root.recover",
        outputs=(output,),
        origin="recovery",
        acceptance_policy=policy,
    )
    stale = Asset(
        id="stale-output",
        name=output,
        content="old",
        created_by="failed-task",
        origin="llm",
    )
    assert all_outputs_satisfied(recovery, _AssetLookup((stale,))) is True
    own = Asset(
        id="own-recovered-output",
        name=output,
        content="repaired",
        created_by=recovery.id,
    )
    assert all_outputs_satisfied(recovery, _AssetLookup((stale, own)))


@pytest.mark.parametrize("origin", ["tool", "mcp", "label_placeholder"])
def test_pending_observation_origins_do_not_satisfy_business_outputs(origin):
    contract = Contract(id="ordinary", outputs=("result",))
    pending = Asset(
        id=f"pending-{origin}", name="result", content="observed", origin=origin
    )

    assert (
        all_outputs_satisfied(contract, _AssetLookup(()), pending_assets=(pending,))
        is False
    )


def test_legacy_extra_output_names_keep_their_existing_name_only_behavior():
    contract = Contract(id="legacy", outputs=("result",))

    assert all_outputs_satisfied(
        contract, _AssetLookup(()), extra_output_names={"result"}
    )


def test_legacy_contract_tree_field_does_not_restrict_name_only_asset_source():
    root = Contract(
        id="root-b",
        outputs=("report",),
        acceptance_policy={"mode": "mechanical", "output_source": "contract_tree"},
    )
    unrelated = Asset(
        id="report-a",
        name="report",
        content="from another root",
        created_by="root-a",
        origin="human",
    )
    own = Asset(
        id="report-b",
        name="report",
        content="from this root",
        created_by="root-b",
        origin="human",
    )

    assert all_outputs_satisfied(root, _AssetLookup((unrelated,)))
    assert all_outputs_satisfied(root, _AssetLookup((unrelated, own)))


def test_legacy_contract_tree_field_does_not_block_extra_names_or_pending_asset():
    root = Contract(
        id="root-b",
        outputs=("report",),
        acceptance_policy={"mode": "mechanical", "output_source": "contract_tree"},
    )
    foreign_pending = Asset(
        id="pending-a",
        name="report",
        content="foreign",
        created_by="root-a",
        origin="human",
    )

    assert all_outputs_satisfied(
        root,
        _AssetLookup(()),
        extra_output_names={"report"},
        pending_assets=(foreign_pending,),
    )


def test_legacy_contract_tree_field_does_not_require_descendant_provenance():
    root = Contract(
        id="root-b",
        outputs=("report",),
        acceptance_policy={"mode": "mechanical", "output_source": "contract_tree"},
    )
    child = Contract(id="child-b", parent_id="root-b")
    output = Asset(
        id="report-child",
        name="report",
        content="child result",
        created_by=child.id,
        origin="human",
    )

    assert all_outputs_satisfied(root, _AssetLookup(()), pending_assets=(output,))


def test_legacy_policy_none_keeps_same_name_compatibility():
    root = Contract(id="root-b", outputs=("report",))
    unrelated = Asset(
        id="report-a",
        name="report",
        content="legacy reusable result",
        created_by="root-a",
        origin="human",
    )

    assert all_outputs_satisfied(root, _AssetLookup((unrelated,)))


def test_output_requirements_match_exact_definition_content_and_producer():
    definition_id = "definition:v1:" + "a" * 64
    content_id = "content:v1:" + "b" * 64
    contract = Contract(
        id="selected-output",
        outputs=("report",),
        acceptance_policy={
            "mode": "mechanical",
            "output_requirements": {
                "report": {
                    "definition_id": definition_id,
                    "content_id": content_id,
                    "producer_actor_ids": ["actor:alice", "actor:bob"],
                }
            },
        },
    )
    validate_output_requirements(contract)
    correct = Asset(
        id="right",
        name="report",
        content="body",
        origin="human",
        definition_hash=definition_id,
        content_hash=content_id,
        signed_by="actor:bob",
    )
    wrong = Asset(
        id="wrong",
        name="report",
        content="body",
        origin="human",
        definition_hash=definition_id,
        content_hash="content:v1:" + "c" * 64,
        signed_by="actor:bob",
    )
    wrong_definition = Asset(
        id="wrong-definition",
        name="report",
        content="body",
        origin="human",
        definition_hash="definition:v1:" + "f" * 64,
        content_hash=content_id,
        signed_by="actor:bob",
    )
    wrong_producer = Asset(
        id="wrong-producer",
        name="report",
        content="body",
        origin="human",
        definition_hash=definition_id,
        content_hash=content_id,
        signed_by="actor:carol",
    )

    assert output_asset_meets_conditions(contract, "report", correct)
    assert not output_asset_meets_conditions(contract, "report", wrong)
    assert not output_asset_meets_conditions(contract, "report", wrong_definition)
    assert not output_asset_meets_conditions(contract, "report", wrong_producer)
    assert (
        unmet_output_names(
            contract, _AssetLookup((wrong, wrong_definition, wrong_producer, correct))
        )
        == ()
    )
    assert not all_outputs_satisfied(contract, _AssetLookup((wrong,)))


def test_unknown_definition_requirement_has_no_name_fallback():
    definition_id = "definition:v1:" + "d" * 64
    contract = Contract(
        id="definition-bound",
        outputs=("report",),
        acceptance_policy={
            "mode": "mechanical",
            "output_requirements": {"report": {"definition_id": definition_id}},
        },
    )
    same_name = Asset(id="same-name", name="report", content="body", origin="human")
    lookup = _AssetLookup((same_name,))

    assert unmet_output_names(contract, lookup) == ("report",)


def test_pending_asset_must_match_explicit_requirement():
    content_id = "content:v1:" + "e" * 64
    contract = Contract(
        id="pending-bound",
        outputs=("report",),
        acceptance_policy={
            "mode": "mechanical",
            "output_requirements": {"report": {"content_id": content_id}},
        },
    )
    pending = Asset(
        id="pending", name="report", content="body", content_hash=content_id
    )

    assert (
        unmet_output_names(contract, _AssetLookup(()), pending_assets=(pending,)) == ()
    )


@pytest.mark.parametrize(
    "selectors",
    [
        {"unrecognized": "ignored-before"},
        {"producer_actor_ids": "actor:alice"},
    ],
)
def test_direct_output_matcher_fails_closed_for_malformed_selectors(selectors):
    contract = Contract(
        id="malformed-direct",
        outputs=("report",),
        acceptance_policy={
            "mode": "mechanical",
            "output_requirements": {"report": selectors},
        },
    )
    asset = Asset(
        id="candidate", name="report", content="body", signed_by="actor:alice"
    )

    assert not output_asset_meets_conditions(contract, "report", asset)
    assert unmet_output_names(contract, _AssetLookup((asset,))) == ("report",)


def test_extra_output_name_cannot_bypass_declared_output_shape():
    contract = Contract(
        id="shaped",
        outputs=("report",),
        acceptance_policy={"mode": "mechanical", "output_shapes": {"report": "string"}},
    )

    assert not all_outputs_satisfied(
        contract, _AssetLookup(()), extra_output_names={"report"}
    )
    invalid = Asset(id="invalid", name="report", content="{}", origin="human")
    assert not all_outputs_satisfied(
        contract, _AssetLookup((invalid,)), extra_output_names={"report"}
    )
    valid = Asset(id="valid", name="report", content='"body"', origin="human")
    assert all_outputs_satisfied(contract, _AssetLookup((valid,)))


@pytest.mark.parametrize(
    "requirements",
    [
        [],
        {"undeclared": {"content_id": "content:v1:" + "a" * 64}},
        {"report": {}},
        {"report": {"content_id": "content:v1:ABC"}},
        {"report": {"producer_actor_ids": []}},
        {"report": {"producer_actor_ids": ["actor:b", "actor:a"]}},
        {"report": {"producer_actor_ids": ["actor:a", "actor:a"]}},
        {"report": {"extra": "not-an-allowed-selector"}},
    ],
)
def test_output_requirements_reject_malformed_schema(requirements):
    contract = Contract(
        id="invalid-requirement",
        outputs=("report",),
        acceptance_policy={"mode": "mechanical", "output_requirements": requirements},
    )

    with pytest.raises(ValueError):
        validate_output_requirements(contract)
    with pytest.raises(ValueError):
        validate_acceptance_policy(contract)


@pytest.mark.parametrize("prefix", NON_OUTPUT_PREFIXES)
def test_context_named_asset_cannot_fill_a_differently_named_business_role(prefix):
    contract = Contract(id="ordinary", outputs=("result",))
    pending = Asset(
        id="pending-prefix", name=f"{prefix}result", content="observed", origin="human"
    )

    assert (
        all_outputs_satisfied(contract, _AssetLookup(()), pending_assets=(pending,))
        is False
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_same_name_pending_values_preserve_valid_and_invalid_assets(reverse):
    contract = Contract(id="ordinary", outputs=("result",))
    good = Asset(id="business", name="result", content="accepted", origin="human")
    bad = Asset(id="observation", name="result", content="observed", origin="tool")
    pending = (bad, good) if reverse else (good, bad)

    assert all_outputs_satisfied(contract, _AssetLookup(()), pending_assets=pending)
    assert not all_outputs_satisfied(contract, _AssetLookup(()), pending_assets=(bad,))
    assert all_outputs_satisfied(contract, _AssetLookup((good,)), pending_assets=(bad,))


@pytest.mark.parametrize("contract_origin", ["system", "recovery"])
def test_protected_output_role_is_available_across_task_origins(contract_origin):
    output = "_plan_result_parent"
    contract = Contract(
        id="owner",
        name="owned-output",
        outputs=(output,),
        origin=contract_origin,
    )
    own = Asset(id="own", name=output, content="owned", created_by=contract.id)
    foreign = Asset(
        id="foreign", name=output, content="foreign", created_by="other", origin="llm"
    )

    assert all_outputs_satisfied(contract, _AssetLookup(()), pending_assets=(own,))
    assert all_outputs_satisfied(contract, _AssetLookup(()), pending_assets=(foreign,))
    assert all_outputs_satisfied(
        contract, _AssetLookup(()), extra_output_names={output}
    )
    strict = Contract(
        id="strict-owner",
        name="strict-output",
        outputs=(output,),
        origin=contract_origin,
        acceptance_policy={"mode": "mechanical", "output_source": "contract_tree"},
    )
    assert all_outputs_satisfied(strict, _AssetLookup(()), extra_output_names={output})


def test_pending_asset_does_not_replace_exact_independent_qualification():
    contract = Contract(
        id="independent-owner",
        outputs=("report",),
        acceptance_policy={"mode": "independent", "policy_version": "review-v1"},
    )
    pending = Asset(
        id="pending-report",
        name="report",
        content="draft",
        created_by=contract.id,
        origin="human",
    )
    exact_asset = Asset(
        id="qualified-report",
        name="report",
        content="reviewed",
        created_by="another-contract-tree",
        origin="human",
    )
    exact_qualification = create_runtime_record(
        "output.qualified",
        {
            "contract_id": contract.id,
            "output_name": "report",
            "asset_id": exact_asset.id,
        },
    )
    wrong_qualification = create_runtime_record(
        "output.qualified",
        {
            "contract_id": contract.id,
            "output_name": "report",
            "asset_id": "another-report",
        },
    )

    assert not all_outputs_satisfied(
        contract, _AssetLookup(()), pending_assets=(pending,)
    )
    assert not all_outputs_satisfied(
        contract,
        _AssetLookup((exact_asset,), (wrong_qualification,)),
        pending_assets=(pending,),
    )
    assert all_outputs_satisfied(
        contract,
        _AssetLookup((exact_asset,), (exact_qualification,)),
        pending_assets=(pending,),
    )
