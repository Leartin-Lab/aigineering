"""Method action to sub-contract conversion."""

from __future__ import annotations

import json

from aigineering.core.ids import asset_id, contract_id
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import contract_to_canonical

_METHOD_OUTPUT_PREFIX: dict[str, str] = {
    "plan": "_plan_result_",
    "replan": "_replan_result_",
    "tool": "_tool_obs_",
}


def method_contract(parent: Contract, action: WorkerAction) -> Contract:
    """Create a system-owned method sub-contract from a parsed action."""

    if action.type not in _METHOD_OUTPUT_PREFIX:
        raise ValueError(f"action '/{action.type}' is not a method action")

    output_name = f"{_METHOD_OUTPUT_PREFIX[action.type]}{parent.id}"
    contract = Contract(
        id="",
        parent_id=parent.id,
        name=f"{parent.name}.{action.type}" if parent.name else action.type,
        description=_method_description(parent, action),
        inputs=list(parent.inputs),
        outputs=[output_name],
        activation=f"_method_ctx_{parent.id}",
        budget=1,
        tool_scope=list(parent.tool_scope),
        labels=list(parent.labels),
        origin="system",
    )
    return Contract(
        id=contract_id(contract_to_canonical(contract)),
        parent_id=contract.parent_id,
        name=contract.name,
        description=contract.description,
        inputs=contract.inputs,
        outputs=contract.outputs,
        activation=contract.activation,
        budget=contract.budget,
        tool_scope=contract.tool_scope,
        labels=contract.labels,
        origin=contract.origin,
    )


def _method_description(parent: Contract, action: WorkerAction) -> str:
    payload = action.payload if action.payload else {"outputs": action.outputs}
    return json.dumps(
        {
            "method": action.type,
            "parent_contract_id": parent.id,
            "parent_contract_name": parent.name,
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def method_payload(contract: Contract) -> dict:
    """Return parsed method metadata from a system method contract."""

    if not contract.description:
        return {}
    try:
        parsed = json.loads(contract.description)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def system_asset(
    name: str,
    content: str,
    created_by: str,
    promptable: bool = True,
    source_uri: str = "",
) -> Asset:
    """Create a deterministic engine-minted system asset."""

    canonical = json.dumps(
        {"name": name, "content": content},
        sort_keys=True,
        ensure_ascii=False,
    )
    return Asset(
        id=asset_id(canonical),
        name=name,
        content=content,
        created_by=created_by,
        origin="system",
        trust_tier="system",
        minted_by="engine",
        source_uri=source_uri,
        promptable=promptable,
    )


def contracts_from_plan_asset(
    asset: Asset,
    parent_id: str | None,
) -> list[Contract]:
    """Expand a `_plan_result_*` asset into non-system child contracts."""

    try:
        payload = json.loads(asset.content)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []

    raw_contracts = payload.get("contracts", [])
    if not isinstance(raw_contracts, list):
        return []

    contracts: list[Contract] = []
    for raw in raw_contracts:
        if not isinstance(raw, dict):
            continue
        contract = Contract(
            id="",
            parent_id=parent_id,
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            inputs=_string_list(raw.get("inputs", [])),
            outputs=_string_list(raw.get("outputs", [])),
            activation=str(raw.get("activation", "")),
            budget=int(raw.get("budget", 1) or 1),
            tool_scope=_string_list(raw.get("tool_scope", [])),
            labels=_string_list(raw.get("labels", [])),
            origin="plan",
        )
        contracts.append(
            Contract(
                id=contract_id(contract_to_canonical(contract)),
                parent_id=contract.parent_id,
                name=contract.name,
                description=contract.description,
                inputs=contract.inputs,
                outputs=contract.outputs,
                activation=contract.activation,
                budget=contract.budget,
                tool_scope=contract.tool_scope,
                labels=contract.labels,
                origin=contract.origin,
            )
        )
    return contracts


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
