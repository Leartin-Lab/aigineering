"""Method action to sub-contract conversion."""

from __future__ import annotations

import json

from aigineering.core.ids import hash_asset_content, hash_asset_definition, hash_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Asset, Contract

_METHOD_OUTPUT_PREFIX: dict[str, str] = {
    "plan": "_plan_result_",
    "replan": "_replan_result_",
    "tool": "_tool_obs_",
}

# Prefixes that planner-children must never declare as outputs.
_PLAN_RESERVED_PREFIXES: frozenset[str] = frozenset(
    {"_sys_", "_skill_", "_memory_", "_mcp_", "_soul_", "_persona_"}
)

# Fields the planner must not set in child contract payloads.
# (origin is always hard-clamped to "plan" by the engine.)
_PLAN_PROTECTED_FIELDS: frozenset[str] = frozenset(
    {"trust_tier", "created_by", "minting_authority"}
)


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
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content),
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
    parent_contract: Contract | None = None,
) -> tuple[list[Contract], list[dict]]:
    """Expand a `_plan_result_*` asset into non-system child contracts.

    When *parent_contract* is provided every child is validated against
    the parent's capability boundary (deny-by-default).  Violations are
    either rejected or clamped and recorded in the returned rejection
    entries.

    Returns (accepted_contracts, rejection_entries).
    """

    try:
        payload = json.loads(asset.content)
    except json.JSONDecodeError:
        return [], []
    if not isinstance(payload, dict):
        return [], []

    raw_contracts = payload.get("contracts", [])
    if not isinstance(raw_contracts, list):
        return [], []

    accepted: list[Contract] = []
    rejected: list[dict] = []

    parent_tools = set(parent_contract.tool_scope) if parent_contract is not None else None
    parent_labels = set(parent_contract.labels) if parent_contract is not None else None
    parent_budget = parent_contract.budget if parent_contract is not None else None

    for raw in raw_contracts:
        if not isinstance(raw, dict):
            continue

        name = str(raw.get("name", ""))

        # --- Deny-by-default: protected fields ---
        if _PLAN_PROTECTED_FIELDS & set(raw.keys()):
            found = sorted(_PLAN_PROTECTED_FIELDS & set(raw.keys()))
            rejected.append(
                {
                    "child_name": name,
                    "field": ",".join(found),
                    "reason": f"planner cannot set {found}",
                    "action": "rejected",
                    "expected": "absent",
                    "actual": f"present ({found})",
                }
            )
            continue

        description = str(raw.get("description", ""))
        inputs = _string_list(raw.get("inputs", []))
        outputs = _string_list(raw.get("outputs", []))
        activation = str(raw.get("activation", ""))
        budget = _positive_int(raw.get("budget", 1), default=1)
        tool_scope = _string_list(raw.get("tool_scope", []))
        labels = _string_list(raw.get("labels", []))
        origin = "plan"

        if parent_contract is None:
            # Backward-compatible path: no validation
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
            accepted.append(
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
            continue

        # --- Tool-scope containment: clamp to parent intersection ---
        if parent_tools is not None:
            child_tools = set(tool_scope)
            if not child_tools.issubset(parent_tools):
                original_scope = sorted(tool_scope)
                tool_scope = sorted(child_tools & parent_tools)
                rejected.append(
                    {
                        "child_name": name,
                        "field": "tool_scope",
                        "reason": (
                            f"tool_scope {original_scope} is not a subset "
                            f"of parent {sorted(parent_tools)}"
                        ),
                        "action": "clamped",
                        "expected": f"subset of {sorted(parent_tools)}",
                        "actual": str(original_scope),
                    }
                )

        # --- Label containment: reject if not subset ---
        if parent_labels is not None and not set(labels).issubset(parent_labels):
            rejected.append(
                {
                    "child_name": name,
                    "field": "labels",
                    "reason": (
                        f"labels {sorted(labels)} are not a subset "
                        f"of parent {sorted(parent_labels)}"
                    ),
                    "action": "rejected",
                    "expected": f"subset of {sorted(parent_labels)}",
                    "actual": str(sorted(labels)),
                }
            )
            continue

        # --- Protected output check ---
        violated_outputs = [
            o for o in outputs
            if any(o.startswith(p) for p in _PLAN_RESERVED_PREFIXES)
        ]
        if violated_outputs:
            rejected.append(
                {
                    "child_name": name,
                    "field": "outputs",
                    "reason": (
                        f"outputs {violated_outputs} use reserved prefixes"
                    ),
                    "action": "rejected",
                    "expected": f"no prefix in {sorted(_PLAN_RESERVED_PREFIXES)}",
                    "actual": str(violated_outputs),
                }
            )
            continue

        # --- Budget fan-out clamp ---
        if parent_budget is not None and budget > parent_budget:
            origin_budget = budget
            budget = parent_budget
            rejected.append(
                {
                    "child_name": name,
                    "field": "budget",
                    "reason": (
                        f"budget {origin_budget} exceeds parent budget {parent_budget}"
                    ),
                    "action": "clamped",
                    "expected": f"<= {parent_budget}",
                    "actual": str(origin_budget),
                }
            )

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
        accepted.append(
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
    return accepted, rejected


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
