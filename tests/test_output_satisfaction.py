"""Output satisfaction must not let stale repair inputs satisfy repair tasks."""

from __future__ import annotations

from collections.abc import Sequence

from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.protocol.types import Asset, Contract


class _AssetLookup:
    def __init__(self, assets: Sequence[Asset]) -> None:
        self.assets = tuple(assets)

    def get_assets_by_name(self, name: str) -> tuple[Asset, ...]:
        return tuple(asset for asset in self.assets if asset.name == name)


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
