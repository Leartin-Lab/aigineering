"""Output satisfaction must not let stale repair inputs satisfy repair tasks."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from aigineering.core.output_satisfaction import (
    NON_OUTPUT_PREFIXES,
    all_outputs_satisfied,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract


class _AssetLookup:
    def __init__(self, assets: Sequence[Asset], records=()) -> None:
        self.assets = tuple(assets)
        self.records = tuple(records)

    def get_assets_by_name(self, name: str) -> tuple[Asset, ...]:
        return tuple(asset for asset in self.assets if asset.name == name)

    def scan_runtime_records(self, *, after_revision=0, record_type=None):
        return tuple(
            (revision, record)
            for revision, record in enumerate(self.records, start=1)
            if revision > after_revision
            and (record_type is None or record.record_type == record_type)
        )

    def get_contract(self, contract_id: str):
        return None


def test_system_repair_requires_its_own_protected_result_asset():
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

    assert all_outputs_satisfied(repair, _AssetLookup((stale,))) is False

    corrected = Asset(
        id="corrected-plan",
        name=output,
        content='{"contracts":[{"name":"child"}]}',
        created_by=repair.id,
        origin="llm",
    )
    assert all_outputs_satisfied(repair, _AssetLookup((stale, corrected))) is True


def test_recovery_task_also_requires_its_own_result_asset():
    output = "recovered_output"
    recovery = Contract(
        id="recovery-task",
        name="root.recover",
        outputs=(output,),
        origin="recovery",
    )
    stale = Asset(
        id="stale-output",
        name=output,
        content="old",
        created_by="failed-task",
        origin="llm",
    )
    assert all_outputs_satisfied(recovery, _AssetLookup((stale,))) is False


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


@pytest.mark.parametrize("prefix", NON_OUTPUT_PREFIXES)
def test_pending_observation_prefixes_do_not_satisfy_business_outputs(prefix):
    output = f"{prefix}result"
    contract = Contract(id="ordinary", outputs=(output,))
    pending = Asset(
        id="pending-prefix", name=output, content="observed", origin="human"
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
def test_pending_system_and_recovery_outputs_require_their_own_creator(contract_origin):
    output = "_plan_result_parent" if contract_origin == "system" else "result"
    contract = Contract(
        id="owner",
        name="owned-output",
        outputs=(output,),
        origin=contract_origin,
    )
    own = Asset(id="own", name=output, content="owned", created_by=contract.id)
    foreign = Asset(id="foreign", name=output, content="foreign", created_by="other")

    assert all_outputs_satisfied(contract, _AssetLookup(()), pending_assets=(own,))
    assert not all_outputs_satisfied(
        contract, _AssetLookup(()), pending_assets=(foreign,)
    )


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
        created_by=contract.id,
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
