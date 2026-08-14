from __future__ import annotations

from aigineering.core.ids import hash_contract_current, validate_contract_identity
from aigineering.core.control_plane import (
    bind_contract_label_assets,
    build_control_plane_contract,
)
from aigineering.core.methods import (
    continuation_contract,
    contracts_from_plan_asset,
    method_contract,
    retry_contract,
)
from aigineering.plugins import PluginRequest, StagedPlanningPlugin
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Asset, Contract
from conftest import candidate_runtime


def _parent() -> Contract:
    fields = {
        "name": "root",
        "description": "Do work",
        "inputs": (),
        "outputs": ("result",),
        "activation": "",
        "budget": 8,
        "tool_scope": (),
        "labels": ("behavior:concise",),
        "worker_capabilities": (),
        "worker_pools": (),
        "origin": "human",
        "context_asset_ids": ("asset:behavior-v1",),
    }
    return Contract(id=hash_contract_current(**fields), **fields)


def _assert_inherits(child: Contract, parent: Contract) -> None:
    assert child.id.startswith("task:v4:")
    assert child.context_asset_ids == parent.context_asset_ids
    validate_contract_identity(child)


def test_v4_context_survives_method_retry_continuation_and_staged_plan() -> None:
    parent = _parent()
    method = method_contract(
        parent, WorkerAction(type="replan", payload={"reason": "split"})
    )
    retry = retry_contract(parent)
    continuation = continuation_contract(
        parent, method, method="replan", budget=parent.budget
    )
    stages = StagedPlanningPlugin().stages(
        PluginRequest(parent=parent, allowance=parent.budget)
    )
    for child in (method, retry, continuation, *stages.contracts):
        _assert_inherits(child, parent)


def test_v4_context_survives_recursive_plan_expansion() -> None:
    parent = _parent()
    plan = Asset(
        id="plan",
        name=f"_plan_result_{parent.id}",
        content=(
            '{"contracts":[{"name":"child","description":"continue",'
            '"inputs":[],"outputs":["result"],"budget":2,'
            '"labels":["behavior:concise"]}]}'
        ),
        origin="system",
    )
    children, rejections = contracts_from_plan_asset(
        plan, parent_id=parent.id, parent_contract=parent
    )
    assert not [item for item in rejections if item.get("action") == "rejected"]
    assert len(children) == 1
    _assert_inherits(children[0], parent)


def test_v4_method_contract_commits_with_audit_method_label(temp_sqlite_store) -> None:
    runtime = candidate_runtime(temp_sqlite_store)
    behavior = runtime.accept_asset(
        Asset(
            id="ignored",
            name="behavior:concise",
            content="Be concise",
            trust_tier="configured",
        )
    )
    parent = bind_contract_label_assets(
        build_control_plane_contract(
            name="root",
            outputs=("result",),
            labels=("behavior:concise",),
        ),
        temp_sqlite_store,
    )
    parent = runtime.accept_contract(parent)
    child = method_contract(
        parent, WorkerAction(type="replan", payload={"reason": "split"})
    )
    assert child.context_asset_ids == (behavior.id,)
    assert runtime.accept_contract(child) == child


def test_v5_identity_binds_execution_and_delegation_scopes(temp_sqlite_store) -> None:
    fields = {
        "name": "heterogeneous-root",
        "description": "Plan work for specialized workers",
        "inputs": (),
        "outputs": ("result",),
        "activation": "",
        "budget": 5,
        "tool_scope": (),
        "labels": (),
        "worker_capabilities": ("planning",),
        "worker_pools": ("orchestrator",),
        "delegation_capabilities": ("text.extract", "reasoning.deep"),
        "delegation_pools": ("economy", "advanced"),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_current(**fields), **fields)

    assert contract.id.startswith("task:v5:")
    validate_contract_identity(contract)
    candidate_runtime(temp_sqlite_store).accept_contract(contract)
    assert temp_sqlite_store.get_contract(contract.id) == contract
    temp_sqlite_store.rebuild_runtime_materializations()
    assert temp_sqlite_store.get_contract(contract.id) == contract
