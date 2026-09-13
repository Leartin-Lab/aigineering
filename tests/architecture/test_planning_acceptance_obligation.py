"""Planning compile stages preserve the ancestor acceptance obligation."""

from __future__ import annotations

from dataclasses import replace

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.ids import contract_identity_v3
from aigineering.core.provenance import sign_asset
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.plugins import (
    PluginRequest,
    StagedPlanningPlugin,
    StagedReplanningPlugin,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset


_POLICY = {
    "mode": "independent",
    "policy_version": "review-v1",
    "required_attestations": 1,
    "verifier_capabilities": ("report.verify",),
    "rubric_asset_ids": ("asset:rubric",),
    "evidence_asset_ids": ("asset:evidence",),
    "output_shapes": {"report": {"type": "nonempty_string"}},
}


def _parent():
    return build_control_plane_contract(
        name="review",
        outputs=("report",),
        budget=8,
        acceptance_policy=_POLICY,
    )


def test_plan_and_replan_compile_inherit_the_complete_acceptance_policy():
    for plugin in (StagedPlanningPlugin(), StagedReplanningPlugin()):
        stages = plugin.stages(PluginRequest(parent=_parent(), allowance=8))

        assert stages.draft.acceptance_policy["mode"] == "mechanical"
        assert stages.dependencies.acceptance_policy["mode"] == "mechanical"
        assert dict(stages.compile.acceptance_policy) == {
            **_POLICY,
            "verifier_capabilities": ("report.verify",),
            "rubric_asset_ids": ("asset:rubric",),
            "evidence_asset_ids": ("asset:evidence",),
            "output_shapes": {"report": {"type": "nonempty_string"}},
        }


def test_compile_waits_for_ancestor_qualification_before_parent_can_cancel_it():
    root = _parent()
    compile_contract = (
        StagedPlanningPlugin().stages(PluginRequest(parent=root, allowance=8)).compile
    )
    gate = build_control_plane_contract(
        name="report-gate",
        inputs=("report",),
        outputs=("receipt",),
        worker_capabilities=("report.verify",),
    )
    gate = replace(gate, parent_id=compile_contract.id)
    gate = replace(gate, id=contract_identity_v3(gate))
    store = MemoryStore()
    for contract in (root, compile_contract, gate):
        store.add_contract(contract)
    report = sign_asset(
        replace(
            Asset(
                id="asset:report",
                name="report",
                content="verified report",
                content_hash="report-hash",
            ),
            created_by=compile_contract.id,
        ),
        "compiler",
    )
    store.add_asset(report)
    reducer = FactReducer(store, MemoryTraceStore())

    before_attestation = reducer.on_assets_created((report,))
    before_types = {(event.type, event.contract_id) for event in before_attestation}
    assert ("contract_complete", compile_contract.id) not in before_types
    assert ("child_cancelled", gate.id) not in before_types

    store.append_runtime_record(
        create_runtime_record(
            "output.qualified",
            {
                "contract_id": root.id,
                "output_name": "report",
                "asset_id": report.id,
                "policy_id": "policy:review-v1",
                "verifier_actor_ids": ["verifier"],
            },
        )
    )
    after_attestation = reducer.on_assets_created((report,))
    after_types = {(event.type, event.contract_id) for event in after_attestation}

    assert ("contract_complete", root.id) in after_types
    assert ("child_cancelled", compile_contract.id) in after_types
    assert ("contract_complete", compile_contract.id) not in after_types
