"""Method action to sub-contract conversion."""

from __future__ import annotations

import json

from aigineering.core.ids import hash_asset_content, hash_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Asset, Contract

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
    contract_name = f"{parent.name}.{action.type}" if parent.name else action.type
    description = _method_description(parent, action)
    outputs = [output_name]
    activation = f"_method_ctx_{parent.id}"
    inputs = list(parent.inputs)
    tool_scope = list(parent.tool_scope)
    labels = list(parent.labels)

    contract_id = hash_contract(
        name=contract_name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=1,
        tool_scope=tool_scope,
        labels=labels,
        origin="system",
    )
    return Contract(
        id=contract_id,
        parent_id=parent.id,
        name=contract_name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=1,
        tool_scope=tool_scope,
        labels=labels,
        origin="system",
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

    return Asset(
        id=hash_asset_content(name, content),
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
        name = str(raw.get("name", ""))
        description = str(raw.get("description", ""))
        inputs = _string_list(raw.get("inputs", []))
        outputs = _string_list(raw.get("outputs", []))
        activation = str(raw.get("activation", ""))
        budget = _positive_int(raw.get("budget", 1), default=1)
        tool_scope = _string_list(raw.get("tool_scope", []))
        labels = _string_list(raw.get("labels", []))
        origin = "plan"

        cid = hash_contract(
            name=name,
            description=description,
            inputs=inputs,
            outputs=outputs,
            activation=activation,
            budget=budget,
            tool_scope=tool_scope,
            labels=labels,
            origin=origin,
        )
        contracts.append(
            Contract(
                id=cid,
                parent_id=parent_id,
                name=name,
                description=description,
                inputs=inputs,
                outputs=outputs,
                activation=activation,
                budget=budget,
                tool_scope=tool_scope,
                labels=labels,
                origin=origin,
            )
        )
    return contracts


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
