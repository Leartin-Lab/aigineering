"""Method action to sub-contract conversion."""

from __future__ import annotations

import json

from aigineering.core.ids import contract_id
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Contract
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
