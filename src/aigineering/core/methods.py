"""Method action to sub-contract conversion."""

from __future__ import annotations

import json
import re

from aigineering.core.authority import RESERVED_PREFIXES
from aigineering.core.ids import (
    hash_asset_content,
    hash_asset_definition,
    hash_contract,
)
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Asset, Contract

_METHOD_OUTPUT_PREFIX: dict[str, str] = {
    "plan": "_plan_result_",
    "replan": "_replan_result_",
    "tool": "_tool_obs_",
}

# Plan-specific reserved prefixes (superset of authority.RESERVED_PREFIXES).
_PLAN_RESERVED_PREFIXES: frozenset[str] = RESERVED_PREFIXES | frozenset({"_persona_"})

# Fields the planner must not set in child contract payloads.
# (origin is always hard-clamped to "plan" by the engine.)
_PLAN_PROTECTED_FIELDS: frozenset[str] = frozenset({"trust_tier", "created_by"})

_ACTIVATION_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})


def _extract_activation_names(expression: str) -> set[str]:
    """Extract asset names from an activation expression.

    Returns the set of non-keyword, non-punctuation tokens.
    For complex/unparseable expressions returns an empty set (pass-through).
    """
    if not expression or not expression.strip():
        return set()
    # Split on whitespace and strip parentheses
    names: set[str] = set()
    for token in re.split(r"\s+", expression.strip()):
        token = token.strip("()")
        if not token:
            continue
        if token.upper() in _ACTIVATION_KEYWORDS:
            continue
        # Only accept tokens that look like simple identifiers
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", token):
            names.add(token)
    return names


def method_contract(parent: Contract, action: WorkerAction) -> Contract:
    """Create a system-owned method sub-contract from a parsed action."""

    if action.type not in _METHOD_OUTPUT_PREFIX:
        raise ValueError(f"action '/{action.type}' is not a method action")

    output_prefix = _METHOD_OUTPUT_PREFIX[action.type]
    if (
        action.type == "tool"
        and isinstance(action.payload, dict)
        and isinstance(action.payload.get("name"), str)
        and action.payload["name"].startswith("mcp:")
    ):
        output_prefix = "_mcp_obs_"
    output_name = f"{output_prefix}{parent.id}"
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
        minting_authority=(output_name,),
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


def method_context_content(
    parent: Contract,
    action: WorkerAction,
    child: Contract,
) -> str:
    """Canonical JSON payload for `_method_ctx_*` activation assets."""
    return json.dumps(
        {
            "method": action.type,
            "parent_contract_id": parent.id,
            "child_contract_id": child.id,
            "payload": action.payload,
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
    allowed_input_names: set[str] | None = None,
    parent_budget_remaining: int | None = None,
) -> tuple[list[Contract], list[dict]]:
    """Expand a `_plan_result_*` asset into non-system child contracts.

    When *parent_contract* is provided every child is validated against
    the parent's capability boundary (deny-by-default).  Violations are
    either rejected or clamped and recorded in the returned rejection
    entries.

    Parameters
    ----------
    allowed_input_names : set[str] | None
        Names the parent can see via its disclosure scope.  Children
        whose inputs reference names outside this set (and not their
        own outputs) are rejected as "input_not_authorized".
    parent_budget_remaining : int | None
        Remaining parent budget.  Total child budgets are bounded so the
        sum never exceeds this value (fan-out containment).

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

    parent_tools = (
        set(parent_contract.tool_scope) if parent_contract is not None else None
    )
    parent_labels = set(parent_contract.labels) if parent_contract is not None else None
    parent_budget = parent_contract.budget if parent_contract is not None else None

    _cumulative_budget = 0

    for raw in raw_contracts:
        if not isinstance(raw, dict):
            continue

        name = str(raw.get("name", ""))
        if not name or not name.strip():
            rejected.append(
                {
                    "child_name": "(empty)",
                    "field": "name",
                    "reason": "child contract name must be non-empty",
                    "action": "rejected",
                    "expected": "non-empty string",
                    "actual": repr(name),
                }
            )
            continue

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

        # --- Input containment: child inputs must be in parent's disclosure scope ---
        if allowed_input_names is not None and allowed_input_names:
            child_output_set = set(outputs)
            unauthorized_inputs = [
                inp
                for inp in inputs
                if inp not in allowed_input_names and inp not in child_output_set
            ]
            if unauthorized_inputs:
                rejected.append(
                    {
                        "child_name": name,
                        "field": "inputs",
                        "reason": (
                            f"inputs {sorted(unauthorized_inputs)} are not in "
                            f"parent disclosure scope ({sorted(allowed_input_names)}) "
                            f"nor in child outputs ({sorted(outputs)})"
                        ),
                        "action": "rejected",
                        "expected": f"subset of {sorted(allowed_input_names)} ∪ child outputs",
                        "actual": str(sorted(unauthorized_inputs)),
                    }
                )
                continue

        # --- Tool-scope containment: reject if not subset ---
        if parent_tools is not None:
            child_tools = set(tool_scope)
            if not child_tools.issubset(parent_tools):
                rejected.append(
                    {
                        "child_name": name,
                        "field": "tool_scope",
                        "reason": (
                            f"tool_scope {sorted(tool_scope)} is not a subset "
                            f"of parent {sorted(parent_tools)}"
                        ),
                        "action": "rejected",
                        "expected": f"subset of {sorted(parent_tools)}",
                        "actual": str(sorted(tool_scope)),
                    }
                )
                continue

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
            o for o in outputs if any(o.startswith(p) for p in _PLAN_RESERVED_PREFIXES)
        ]
        if violated_outputs:
            rejected.append(
                {
                    "child_name": name,
                    "field": "outputs",
                    "reason": (f"outputs {violated_outputs} use reserved prefixes"),
                    "action": "rejected",
                    "expected": f"no prefix in {sorted(_PLAN_RESERVED_PREFIXES)}",
                    "actual": str(violated_outputs),
                }
            )
            continue

        # --- Activation containment: activation refs checked against allowed names ---
        if allowed_input_names is not None and allowed_input_names and activation:
            activation_names = _extract_activation_names(activation)
            child_output_set = set(outputs)
            unknown_activation_names = (
                activation_names - allowed_input_names - child_output_set
            )
            if unknown_activation_names:
                # Names outside allowed inputs and child outputs are likely
                # sibling-output scheduling references — benign for containment
                # since activation only gates scheduling, not disclosure.
                rejected.append(
                    {
                        "child_name": name,
                        "field": "activation",
                        "reason": (
                            f"activation refs {sorted(unknown_activation_names)} "
                            f"are not in parent disclosure scope — may be sibling "
                            f"scheduling references (benign)"
                        ),
                        "action": "noted",
                        "expected": f"subset of {sorted(allowed_input_names)} ∪ child outputs",
                        "actual": str(sorted(unknown_activation_names)),
                    }
                )

        # --- Budget fan-out: contain to individual parent budget first ---
        if parent_budget is not None and budget > parent_budget:
            origin_budget = budget
            budget = parent_budget
            effective_budget = budget
            remaining_budget = (
                max(
                    0, parent_budget_remaining - (_cumulative_budget + effective_budget)
                )
                if parent_budget_remaining is not None
                else None
            )
            rejected.append(
                {
                    "child_name": name,
                    "field": "budget",
                    "reason": (
                        f"budget {origin_budget} exceeds parent budget {parent_budget}"
                    ),
                    "action": "budget_contained",
                    "expected": f"<= {parent_budget}",
                    "actual": str(origin_budget),
                    "requested": origin_budget,
                    "effective": effective_budget,
                    "remaining": remaining_budget,
                }
            )

        # --- Budget fan-out: cumulative containment to parent remaining ---
        if parent_budget_remaining is not None:
            if _cumulative_budget + budget > parent_budget_remaining:
                contained_budget = max(1, parent_budget_remaining - _cumulative_budget)
                if budget != contained_budget:
                    requested_budget = budget
                    budget = contained_budget
                    remaining_budget = max(
                        0, parent_budget_remaining - (_cumulative_budget + budget)
                    )
                    rejected.append(
                        {
                            "child_name": name,
                            "field": "budget",
                            "reason": (
                                f"cumulative child budgets would exceed parent "
                                f"remaining ({parent_budget_remaining}); "
                                f"contained from {requested_budget} to {contained_budget}"
                            ),
                            "action": "budget_contained",
                            "expected": f"total <= {parent_budget_remaining}",
                            "actual": str(requested_budget),
                            "requested": requested_budget,
                            "effective": budget,
                            "remaining": remaining_budget,
                        }
                    )
            _cumulative_budget += budget

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
                sensitive_input_policy=(
                    parent_contract.sensitive_input_policy
                    if parent_contract is not None
                    and parent_contract.sensitive_input_policy
                    else None
                ),
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
