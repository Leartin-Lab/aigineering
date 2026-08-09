"""Pure task projection semantics used by Candidate-native plugins."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aigineering.core.activation import (
    activation_names,
    validate_execution_activation,
)
from aigineering.core.ids import (
    CONTRACT_SELF_REFERENCE,
    hash_asset_content,
    hash_asset_definition,
    hash_contract_current,
)
from aigineering.plugins.plan_scaffold import (
    PLAN_RESERVED_PREFIXES,
    _scaffold_tasks_to_raw_dicts,
    compile_placeholder_names,
    parse_plan_scaffold,
    validate_plan_scaffold,
)
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset, Contract

_METHOD_OUTPUT_PREFIX: dict[str, str] = {
    "plan": "_plan_result_",
    "replan": "_replan_result_",
    "tool": "_tool_obs_",
    "fail": "_fail_result_",
}
_METHOD_LABEL_PREFIX = "method:"

# Fields the planner must not set in child contract payloads.
# (origin is always hard-clamped to "plan" by the engine.)
_PLAN_PROTECTED_FIELDS: frozenset[str] = frozenset(
    {"trust_tier", "created_by", "worker_capabilities", "worker_pools"}
)


def method_contract(parent: Contract, action: WorkerAction) -> Contract:
    """Create a system-owned method sub-contract from a parsed action."""

    if action.type not in _METHOD_OUTPUT_PREFIX:
        raise ValueError(f"action '/{action.type}' is not a method action")

    output_prefix = _METHOD_OUTPUT_PREFIX[action.type]
    if (
        action.type == "tool"
        and isinstance(action.payload, Mapping)
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
    if action.type == "tool" and isinstance(action.payload.get("name"), str):
        tool_name = action.payload["name"]
        descriptor_name = (
            f"_mcp_{tool_name[4:].split('.', 1)[0]}"
            if tool_name.startswith("mcp:")
            else f"_tool_capability_{tool_name}"
        )
        if descriptor_name not in inputs:
            inputs.append(descriptor_name)
    tool_scope = list(parent.tool_scope)
    labels = _append_method_label(parent.labels, action.type)
    worker_capabilities = list(parent.worker_capabilities)
    if action.type == "tool":
        execution_capability = (
            "mcp-execution"
            if isinstance(action.payload.get("name"), str)
            and action.payload["name"].startswith("mcp:")
            else "tool-execution"
        )
        if execution_capability not in worker_capabilities:
            worker_capabilities.append(execution_capability)

    context_name = f"_method_ctx_{parent.id}"
    authority_templates: tuple[str, ...] = (
        output_name,
        context_name,
        f"_fail_context_{CONTRACT_SELF_REFERENCE}",
    )
    if action.type == "fail":
        authority_templates = (
            output_name,
            context_name,
            f"_fail_report_{CONTRACT_SELF_REFERENCE}",
        )
    elif action.type == "tool":
        call_prefix = "_mcp_call_" if output_prefix == "_mcp_obs_" else "_tool_call_"
        authority_templates = (
            output_name,
            context_name,
            f"{call_prefix}{CONTRACT_SELF_REFERENCE}",
        )
    contract_id = hash_contract_current(
        name=contract_name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=1,
        tool_scope=tool_scope,
        labels=labels,
        worker_capabilities=worker_capabilities,
        worker_pools=parent.worker_pools,
        origin="system",
        parent_id=parent.id,
        minting_authority=authority_templates,
        sensitive_input_policy=(
            dict(parent.sensitive_input_policy)
            if parent.sensitive_input_policy is not None
            else None
        ),
        context_asset_ids=parent.context_asset_ids,
    )

    # Expand minting_authority for method-type-specific system assets.
    _extra_authority: tuple[str, ...] = (f"_fail_context_{contract_id}",)
    if action.type == "fail":
        _extra_authority = (f"_fail_report_{contract_id}",)
    elif action.type == "tool":
        if output_prefix == "_mcp_obs_":
            _extra_authority = (f"_mcp_call_{contract_id}",)
        else:
            _extra_authority = (f"_tool_call_{contract_id}",)

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
        context_asset_ids=parent.context_asset_ids,
        worker_capabilities=worker_capabilities,
        worker_pools=parent.worker_pools,
        origin="system",
        # A method contract needs exact authority for both its declared
        # result and the context asset that activates it.  Do not rely on a
        # generic protected-name override when scheduling methods.
        minting_authority=(output_name, context_name) + _extra_authority,
        sensitive_input_policy=parent.sensitive_input_policy,
    )


def retry_contract(parent: Contract) -> Contract:
    """Create a security-equivalent replacement Contract for one failed attempt."""
    name = f"{parent.name or parent.id}.retry"
    authority = tuple(
        output for output in parent.outputs if output in parent.minting_authority
    )
    policy = (
        dict(parent.sensitive_input_policy)
        if parent.sensitive_input_policy is not None
        else None
    )
    contract_id = hash_contract_current(
        name=name,
        description=parent.description,
        inputs=parent.inputs,
        outputs=parent.outputs,
        activation=parent.activation,
        budget=parent.budget,
        tool_scope=parent.tool_scope,
        labels=parent.labels,
        worker_capabilities=parent.worker_capabilities,
        worker_pools=parent.worker_pools,
        origin="retry",
        parent_id=parent.parent_id,
        minting_authority=authority,
        sensitive_input_policy=policy,
        context_asset_ids=parent.context_asset_ids,
    )
    return Contract(
        id=contract_id,
        parent_id=parent.parent_id,
        name=name,
        description=parent.description,
        inputs=parent.inputs,
        outputs=parent.outputs,
        activation=parent.activation,
        budget=parent.budget,
        tool_scope=parent.tool_scope,
        labels=parent.labels,
        context_asset_ids=parent.context_asset_ids,
        worker_capabilities=parent.worker_capabilities,
        worker_pools=parent.worker_pools,
        origin="retry",
        minting_authority=authority,
        sensitive_input_policy=parent.sensitive_input_policy,
    )


def continuation_contract(
    parent: Contract,
    source_contract: Contract,
    *,
    method: str,
    budget: int,
) -> Contract:
    """Create the immutable follow-up attempt after a completed method task."""
    effective_budget = max(1, budget)
    name = f"{parent.name or parent.id}.{method}.continue.{source_contract.id}"
    authority = tuple(
        output for output in parent.outputs if output in parent.minting_authority
    )
    policy = (
        dict(parent.sensitive_input_policy)
        if parent.sensitive_input_policy is not None
        else None
    )
    contract_id = hash_contract_current(
        name=name,
        description=parent.description,
        inputs=[],
        outputs=parent.outputs,
        activation="",
        budget=effective_budget,
        tool_scope=parent.tool_scope,
        labels=parent.labels,
        worker_capabilities=parent.worker_capabilities,
        worker_pools=parent.worker_pools,
        origin="continuation",
        parent_id=parent.id,
        minting_authority=authority,
        sensitive_input_policy=policy,
        context_asset_ids=parent.context_asset_ids,
    )
    return Contract(
        id=contract_id,
        parent_id=parent.id,
        name=name,
        description=parent.description,
        outputs=parent.outputs,
        activation="",
        budget=effective_budget,
        tool_scope=parent.tool_scope,
        labels=parent.labels,
        context_asset_ids=parent.context_asset_ids,
        worker_capabilities=parent.worker_capabilities,
        worker_pools=parent.worker_pools,
        origin="continuation",
        minting_authority=authority,
        sensitive_input_policy=parent.sensitive_input_policy,
    )


def _method_description(parent: Contract, action: WorkerAction) -> str:
    payload = action.payload if action.payload else {"outputs": action.outputs}
    return json.dumps(
        {
            "method": action.type,
            "parent_contract_id": parent.id,
            "parent_contract_name": parent.name,
            "parent_inputs": list(parent.inputs),
            "parent_outputs": list(parent.outputs),
            "parent_tool_scope": list(parent.tool_scope),
            "payload": deep_thaw(payload),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _append_method_label(labels: tuple[str, ...], action_type: str) -> list[str]:
    method_label = f"{_METHOD_LABEL_PREFIX}{action_type}"
    merged = list(labels)
    if method_label not in merged:
        merged.append(method_label)
    return merged


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
            "payload": deep_thaw(action.payload),
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
    origin: str = "system",
    trust_tier: str = "system",
    minted_by: str = "engine",
) -> Asset:
    """Create an authorized runtime asset.

    Reserved namespace authority and source trust are separate dimensions:
    external observations may use a protected runtime name while remaining
    OBSERVED rather than being elevated to SYSTEM.
    """

    return Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content),
        created_by=created_by,
        origin=origin,
        trust_tier=trust_tier,
        minted_by=minted_by,
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

    raw_contracts, parse_errors = _raw_contracts_from_plan_asset(asset, parent_contract)
    if parse_errors:
        return [], parse_errors

    accepted: list[Contract] = []
    rejected: list[dict] = []

    parent_tools = (
        set(parent_contract.tool_scope) if parent_contract is not None else None
    )
    parent_budget = parent_contract.budget if parent_contract is not None else None
    sibling_promises = _accepted_sibling_output_promises(
        raw_contracts,
        parent_tools=parent_tools,
        parent_contract=parent_contract,
        allowed_input_names=allowed_input_names,
    )

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
        if not description.strip():
            rejected.append(
                {
                    "child_name": name,
                    "field": "description",
                    "reason": "planned child must include executable instructions",
                    "action": "rejected",
                    "expected": "non-empty task description",
                    "actual": repr(description),
                    "recoverable": True,
                }
            )
            continue
        inputs = _string_list(raw.get("inputs", []))
        outputs = _string_list(raw.get("outputs", []))
        if not outputs:
            rejected.append(
                {
                    "child_name": name,
                    "field": "outputs",
                    "reason": "planned child must declare at least one output",
                    "action": "rejected",
                    "expected": "non-empty list of asset names",
                    "actual": repr(raw.get("outputs")),
                    "recoverable": True,
                }
            )
            continue
        activation = str(raw.get("activation", ""))
        budget = _positive_int(raw.get("budget", 1), default=1)
        tool_scope = _string_list(raw.get("tool_scope", []))
        labels = _string_list(raw.get("labels", []))

        try:
            validate_execution_activation(activation)
        except (RecursionError, ValueError) as exc:
            rejected.append(
                {
                    "child_name": name,
                    "field": "activation",
                    "reason": f"invalid activation expression: {exc}",
                    "action": "rejected",
                    "expected": "monotonic boolean expression using AND/OR",
                    "actual": activation,
                    "recoverable": True,
                }
            )
            continue

        if parent_contract is None:
            # Backward-compatible path: no validation
            accepted.append(
                _build_plan_contract(
                    parent_id=parent_id,
                    parent_contract=None,
                    name=name,
                    description=description,
                    inputs=inputs,
                    outputs=outputs,
                    activation=activation,
                    budget=budget,
                    tool_scope=tool_scope,
                    labels=labels,
                )
            )
            continue

        blocking, notes = _plan_child_scope_findings(
            name=name,
            inputs=inputs,
            outputs=outputs,
            activation=activation,
            tool_scope=tool_scope,
            parent_tools=parent_tools,
            allowed_input_names=allowed_input_names,
            sibling_promises=sibling_promises,
        )
        rejected.extend(notes)
        if blocking is not None:
            rejected.append(blocking)
            continue

        budget, _cumulative_budget, budget_findings = _contain_plan_budget(
            name=name,
            requested=budget,
            parent_budget=parent_budget,
            parent_budget_remaining=parent_budget_remaining,
            cumulative_budget=_cumulative_budget,
        )
        rejected.extend(budget_findings)
        if budget is None:
            continue

        accepted.append(
            _build_plan_contract(
                parent_id=parent_id,
                parent_contract=parent_contract,
                name=name,
                description=description,
                inputs=inputs,
                outputs=outputs,
                activation=activation,
                budget=budget,
                tool_scope=tool_scope,
                labels=labels,
            )
        )
    if parent_contract is not None and allowed_input_names is not None:
        accepted, dependency_rejections = _retain_reachable_plan_contracts(
            accepted,
            allowed_input_names=allowed_input_names,
        )
        rejected.extend(dependency_rejections)
    if parent_contract is not None and parent_contract.outputs:
        promised_outputs = {output for child in accepted for output in child.outputs}
        outstanding = set(parent_contract.outputs) - promised_outputs
        if outstanding:
            rejected.append(
                {
                    "child_name": "(parent)",
                    "field": "output_recommitment",
                    "reason": (
                        f"parent outputs {sorted(outstanding)} are not promised "
                        "by any accepted child"
                    ),
                    "action": "rejected",
                    "expected": (
                        f"all parent outputs {sorted(parent_contract.outputs)} promised"
                    ),
                    "actual": f"missing: {sorted(outstanding)}",
                    "recoverable": True,
                }
            )
    return accepted, rejected


def _retain_reachable_plan_contracts(
    contracts: list[Contract],
    *,
    allowed_input_names: set[str],
) -> tuple[list[Contract], list[dict]]:
    """Keep only children reachable from facts the parent can actually disclose.

    Preliminary sibling promises make plan validation order-independent, but a
    producer may later fail another containment check or run out of allowance.
    This final fixed point is computed from accepted children only, preventing
    those rejected promises from leaving descendants permanently blocked.
    """
    reachable = set(allowed_input_names)
    pending = list(contracts)
    retained: list[Contract] = []
    while pending:
        progressed = False
        for child in tuple(pending):
            dependencies = set(child.inputs) | activation_names(child.activation)
            if dependencies.issubset(reachable):
                pending.remove(child)
                retained.append(child)
                reachable.update(child.outputs)
                progressed = True
        if not progressed:
            break

    rejections = [
        {
            "child_name": child.name,
            "field": "dependencies",
            "reason": (
                "planned child is not reachable from parent disclosure or outputs "
                "of accepted reachable siblings"
            ),
            "action": "rejected",
            "expected": f"dependencies reachable from {sorted(reachable)}",
            "actual": str(
                sorted(
                    (set(child.inputs) | activation_names(child.activation)) - reachable
                )
            ),
            "recoverable": True,
        }
        for child in pending
    ]
    return retained, rejections


def _raw_contracts_from_plan_asset(
    asset: Asset, parent_contract: Contract | None
) -> tuple[list[object], list[dict]]:
    """Parse legacy or staged plan output without applying child authority."""
    try:
        payload = json.loads(asset.content)
    except json.JSONDecodeError as exc:
        return [], [
            {
                "child_name": "(plan_result)",
                "field": "content",
                "reason": f"plan result content is not valid JSON: {exc}",
                "action": "rejected",
                "expected": "JSON object with 'contracts' or scaffold fields",
                "actual": asset.content[:200],
                "recoverable": True,
            }
        ]
    if not isinstance(payload, dict):
        return [], [
            {
                "child_name": "(plan_result)",
                "field": "content",
                "reason": "plan result content must be a JSON object",
                "action": "rejected",
                "expected": "JSON object with 'contracts' or scaffold fields",
                "actual": type(payload).__name__,
                "recoverable": True,
            }
        ]

    scaffold = parse_plan_scaffold(asset)
    if scaffold is not None:
        scaffold = compile_placeholder_names(scaffold)
        errors = validate_plan_scaffold(scaffold, parent_contract)
        if errors:
            return [], errors
        raw_contracts: list[object] = list(_scaffold_tasks_to_raw_dicts(scaffold))
        raw_contracts.extend(scaffold.final_contracts)
        return raw_contracts, []

    raw_contracts = payload.get("contracts", [])
    if "contracts" not in payload:
        return [], [
            {
                "child_name": "(plan_result)",
                "field": "schema",
                "reason": (
                    "unsupported plan result schema; expected legacy "
                    "'contracts' list or structured scaffold fields"
                ),
                "action": "rejected",
                "expected": "JSON object with 'contracts' or scaffold fields",
                "actual": str(sorted(payload.keys())),
                "recoverable": True,
            }
        ]
    if not isinstance(raw_contracts, list):
        return [], [
            {
                "child_name": "(plan_result)",
                "field": "contracts",
                "reason": "plan result 'contracts' field must be a list",
                "action": "rejected",
                "expected": "list of child contract objects",
                "actual": type(raw_contracts).__name__,
                "recoverable": True,
            }
        ]
    return raw_contracts, []


def _contain_plan_budget(
    *,
    name: str,
    requested: int,
    parent_budget: int | None,
    parent_budget_remaining: int | None,
    cumulative_budget: int,
) -> tuple[int | None, int, list[dict]]:
    """Contain one child allowance without ever exceeding the parent fund."""
    budget = requested
    findings: list[dict] = []
    if parent_budget is not None and budget > parent_budget:
        budget = parent_budget
        findings.append(
            {
                "child_name": name,
                "field": "budget",
                "reason": f"budget {requested} exceeds parent budget {parent_budget}",
                "action": "budget_contained",
                "expected": f"<= {parent_budget}",
                "actual": str(requested),
                "requested": requested,
                "effective": budget,
                "remaining": (
                    max(
                        0,
                        parent_budget_remaining - (cumulative_budget + budget),
                    )
                    if parent_budget_remaining is not None
                    else None
                ),
            }
        )
    if parent_budget_remaining is None:
        return budget, cumulative_budget, findings

    available = max(0, parent_budget_remaining - cumulative_budget)
    if available == 0:
        findings.append(
            {
                "child_name": name,
                "field": "budget",
                "reason": "no parent budget remains for this child",
                "action": "rejected",
                "expected": f"total <= {parent_budget_remaining}",
                "actual": str(budget),
                "requested": requested,
                "effective": 0,
                "remaining": 0,
                "recoverable": True,
            }
        )
        return None, cumulative_budget, findings
    if budget > available:
        previous = budget
        budget = available
        findings.append(
            {
                "child_name": name,
                "field": "budget",
                "reason": (
                    "cumulative child budgets would exceed parent remaining "
                    f"({parent_budget_remaining}); contained from {previous} to {budget}"
                ),
                "action": "budget_contained",
                "expected": f"total <= {parent_budget_remaining}",
                "actual": str(previous),
                "requested": previous,
                "effective": budget,
                "remaining": 0,
            }
        )
    return budget, cumulative_budget + budget, findings


def _plan_child_scope_findings(
    *,
    name: str,
    inputs: list[str],
    outputs: list[str],
    activation: str,
    tool_scope: list[str],
    parent_tools: set[str] | None,
    allowed_input_names: set[str] | None,
    sibling_promises: set[str],
) -> tuple[dict | None, list[dict]]:
    """Return one blocking containment finding plus non-blocking notes."""
    child_outputs = set(outputs)
    if allowed_input_names is not None:
        reachable = allowed_input_names | sibling_promises | child_outputs
        unauthorized = [name for name in inputs if name not in reachable]
        if unauthorized:
            return (
                {
                    "child_name": name,
                    "field": "inputs",
                    "reason": (
                        f"inputs {sorted(unauthorized)} are not in parent disclosure "
                        f"scope ({sorted(allowed_input_names)}) nor promised sibling "
                        f"outputs ({sorted(sibling_promises)}) nor in child outputs "
                        f"({sorted(outputs)})"
                    ),
                    "action": "rejected",
                    "expected": (
                        f"subset of {sorted(allowed_input_names)} ∪ sibling promises "
                        f"{sorted(sibling_promises)} ∪ child outputs"
                    ),
                    "actual": str(sorted(unauthorized)),
                },
                [],
            )
    if parent_tools is not None and not set(tool_scope).issubset(parent_tools):
        return (
            {
                "child_name": name,
                "field": "tool_scope",
                "reason": (
                    f"tool_scope {sorted(tool_scope)} is not a subset of parent "
                    f"{sorted(parent_tools)}"
                ),
                "action": "rejected",
                "expected": f"subset of {sorted(parent_tools)}",
                "actual": str(sorted(tool_scope)),
            },
            [],
        )
    protected_outputs = [
        output
        for output in outputs
        if any(output.startswith(prefix) for prefix in PLAN_RESERVED_PREFIXES)
    ]
    if protected_outputs:
        return (
            {
                "child_name": name,
                "field": "outputs",
                "reason": f"outputs {protected_outputs} use reserved prefixes",
                "action": "rejected",
                "expected": f"no prefix in {sorted(PLAN_RESERVED_PREFIXES)}",
                "actual": str(protected_outputs),
            },
            [],
        )
    if allowed_input_names is None or not activation:
        return None, []
    unknown = activation_names(activation) - (
        allowed_input_names | sibling_promises | child_outputs
    )
    if not unknown:
        return None, []
    return None, [
        {
            "child_name": name,
            "field": "activation",
            "reason": (
                f"activation refs {sorted(unknown)} are not in parent disclosure "
                "scope — may be sibling scheduling references (benign)"
            ),
            "action": "noted",
            "expected": (
                f"subset of {sorted(allowed_input_names)} ∪ sibling promises "
                f"{sorted(sibling_promises)} ∪ child outputs"
            ),
            "actual": str(sorted(unknown)),
        }
    ]


def _build_plan_contract(
    *,
    parent_id: str | None,
    parent_contract: Contract | None,
    name: str,
    description: str,
    inputs: list[str],
    outputs: list[str],
    activation: str,
    budget: int,
    tool_scope: list[str],
    labels: list[str],
) -> Contract:
    worker_capabilities = (
        parent_contract.worker_capabilities if parent_contract is not None else ()
    )
    worker_pools = parent_contract.worker_pools if parent_contract is not None else ()
    sensitive_policy = (
        parent_contract.sensitive_input_policy if parent_contract is not None else None
    )
    context_asset_ids = (
        parent_contract.context_asset_ids if parent_contract is not None else ()
    )
    identity = hash_contract_current(
        name=name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=budget,
        tool_scope=tool_scope,
        labels=labels,
        worker_capabilities=worker_capabilities,
        worker_pools=worker_pools,
        origin="plan",
        parent_id=parent_id,
        sensitive_input_policy=(
            dict(sensitive_policy) if sensitive_policy is not None else None
        ),
        context_asset_ids=context_asset_ids,
    )
    return Contract(
        id=identity,
        parent_id=parent_id,
        name=name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=budget,
        tool_scope=tool_scope,
        labels=labels,
        context_asset_ids=context_asset_ids,
        worker_capabilities=worker_capabilities,
        worker_pools=worker_pools,
        origin="plan",
        sensitive_input_policy=sensitive_policy,
    )


def _accepted_sibling_output_promises(
    raw_contracts: list[object],
    *,
    parent_tools: set[str] | None,
    parent_contract: Contract | None,
    allowed_input_names: set[str] | None,
) -> set[str]:
    """Return outputs from siblings reachable from parent disclosure.

    The promise set is a fixed point: a child contributes its outputs only after
    it passes independent containment and its inputs are reachable from parent
    disclosure, its own outputs, or promises contributed by earlier fixed-point
    rounds. This keeps batch ordering irrelevant without letting rejected
    producers launder hidden input names.
    """

    if parent_contract is None:
        return set()

    promises: set[str] = set()
    candidates = [
        raw
        for raw in raw_contracts
        if _can_contribute_sibling_promises(raw, parent_tools=parent_tools)
    ]
    if allowed_input_names is None:
        for raw in candidates:
            promises.update(_string_list(raw.get("outputs", [])))
        return promises

    changed = True
    while changed:
        changed = False
        for raw in candidates:
            outputs = set(_string_list(raw.get("outputs", [])))
            if outputs.issubset(promises):
                continue
            inputs = set(_string_list(raw.get("inputs", [])))
            activation_refs = activation_names(str(raw.get("activation", "")))
            reachable = allowed_input_names | promises | outputs
            if inputs.issubset(reachable) and activation_refs.issubset(reachable):
                promises.update(outputs)
                changed = True
    return promises


def _can_contribute_sibling_promises(
    raw: object,
    *,
    parent_tools: set[str] | None,
) -> bool:
    if not isinstance(raw, dict):
        return False
    name = str(raw.get("name", ""))
    if not name or not name.strip():
        return False
    if _PLAN_PROTECTED_FIELDS & set(raw.keys()):
        return False

    try:
        validate_execution_activation(str(raw.get("activation", "")))
    except (RecursionError, ValueError):
        return False

    tool_scope = _string_list(raw.get("tool_scope", []))
    outputs = _string_list(raw.get("outputs", []))

    if parent_tools is not None and not set(tool_scope).issubset(parent_tools):
        return False
    return not any(
        output.startswith(prefix)
        for output in outputs
        for prefix in PLAN_RESERVED_PREFIXES
    )


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
