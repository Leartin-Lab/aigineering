"""Prompt construction for candidate-producing workers."""

from __future__ import annotations

import json

from aigineering.core.labels import BEHAVIOR_LABEL_PREFIX
from aigineering.protocol.types import Asset, Contract

SKILL_CONTENT_PREFIX = "_skill_content_"


def system_prompt() -> str:
    """Return the invariant-preserving system prompt."""

    return (
        "You are an Aigineering worker. Your output is only a candidate, "
        "not committed state. Return exactly one structured action. To "
        'produce final outputs, use `/exec {"outputs": {"asset_name": '
        '"content"}}`. Use only declared output names. If you need task '
        "decomposition because the current task needs more information, use "
        '`/plan {"reason": "..."}`. If the current task has already gone '
        "off course because an assumption, path, or result is invalid, use "
        '`/replan {"reason": "..."}`. If you need an allowed tool, use '
        '`/tool {"name": "...", "args": {}}`. '
        "When the provider can request independent tools in parallel, multiple "
        "tool calls are compiled into one /parallel_tool method and ordinary "
        "tool tasks. If the same task can be "
        "attempted again after a transient execution or output failure, use "
        '`/retry {"reason": "..."}`. If required evidence is unavailable and '
        "no safe plan or allowed tool can obtain it, or completing the task "
        "would require fabricated facts, use "
        '`/fail {"reason": "..."}`. Do not use /replan for missing '
        "information, and do not use /retry when new evidence or a different "
        "plan is required. Do not add markdown, explanations, or undeclared "
        "assets."
    )


def contract_prompt(contract: Contract, assets: list[Asset]) -> str:
    """Render a contract and disclosed assets for a worker."""

    behavior_assets = [
        asset for asset in assets if asset.name.startswith(BEHAVIOR_LABEL_PREFIX)
    ]
    skill_assets = [
        asset for asset in assets if asset.name.startswith(SKILL_CONTENT_PREFIX)
    ]
    evidence_assets = [
        asset
        for asset in assets
        if not asset.name.startswith((BEHAVIOR_LABEL_PREFIX, SKILL_CONTENT_PREFIX))
    ]

    lines = [
        f"Contract name: {contract.name}",
        f"Description: {contract.description}",
        "Declared inputs: " + ", ".join(contract.inputs),
        "Declared outputs: " + ", ".join(contract.outputs),
        "Allowed tools: " + ", ".join(contract.tool_scope),
        f"Available causal allowance: {contract.budget}",
        "",
        "Return format:",
        '- /exec {"outputs": {"declared_output": "content"}}',
        '- /plan {"reason": "why the current task needs more information"}',
        '- /replan {"reason": "why the current task has already gone off course"}',
        '- /tool {"name": "tool_name", "args": {}}',
        '- /parallel_tool {"calls": [{"id": "a", "name": "tool_name", "args": {}}], "join": "all"}',
        '- /retry {"reason": "transient failure that permits the same task to be attempted again"}',
        '- /fail {"reason": "why the task cannot be completed safely"}',
        "",
        "Decision boundary:",
        "- Every asset listed under Disclosed assets includes its exact readable content; treat that content as directly available evidence, not as a path or handle requiring another tool.",
        "- A successful tool observation is completed evidence. Satisfy the declared output from it; the same tool is removed from the continuation scope. Publish separate tasks when repeated calls are required.",
        "- Use /plan when disclosed information is insufficient.",
        "- /plan and /replan create three planning-stage tasks and therefore require at least 3 causal allowance units.",
        "- Use /replan only after an assumption, path, or result is invalid.",
        "- Do not use /replan for missing information.",
        "- Use /retry only for a transient execution or output failure when the same task can be attempted again.",
        "- Do not use /retry when new evidence or a different plan is required.",
        "- Use /fail when evidence cannot be obtained safely or completion would require fabricated facts.",
        "",
    ]
    lines.extend(_method_result_instructions(contract))
    lines.extend(_planning_stage_instructions(contract))
    lines.extend(_generated_task_instructions(contract))
    lines.extend(
        [
            "Behavior instructions:",
        ]
    )
    for asset in behavior_assets:
        lines.append(f"- {asset.name}: {asset.content}")

    lines.extend(["", "Skill instructions:"])
    for asset in skill_assets:
        lines.append(f"- {asset.name}: {asset.content}")

    lines.extend(
        [
            "",
            "Disclosed assets:",
        ]
    )
    for asset in evidence_assets:
        lines.append(f"- {asset.name}: {asset.content}")
    return "\n".join(lines)


def _method_result_instructions(contract: Contract) -> list[str]:
    """Add an explicit schema only for planner method-result contracts.

    A method result is still emitted through the normal `/exec` action, but
    its declared protected output carries a JSON plan.  Without this schema a
    real model reasonably returns ordinary prose or an unsupported action.
    """
    plan_outputs = [
        output
        for output in contract.outputs
        if output.startswith("_plan_result_") or output.startswith("_replan_result_")
    ]
    if not plan_outputs:
        return []
    output = plan_outputs[0]
    return [
        "Planner result protocol (required):",
        f"- Return /exec with exactly one output named `{output}`.",
        "- Its content must be one JSON object with a `contracts` array.",
        "- Each child may contain only: name, description, inputs, outputs, activation, budget, tool_scope, labels, capability_needs, pool_needs, delegation_capabilities, delegation_pools.",
        "- Use only disclosed input names, declared parent tools, and simple unqualified asset names in activation.",
        "- Activation is a boolean expression: join multiple required inputs with uppercase AND (for example `input_a AND input_b`); never use commas, JSON lists, or whitespace as an operator.",
        "- The method description lists parent_outputs. At least one child must produce every parent output; use intermediate outputs only when a later child consumes them.",
        "- Do not emit origin, trust_tier, created_by, minting_authority, worker_capabilities, worker_pools, or prose outside the action.",
        '- A minimal valid result is: /exec {"outputs": {"'
        + output
        + '": "{\\"contracts\\":[{\\"name\\":\\"extract\\",\\"description\\":\\"extract facts\\",\\"inputs\\":[\\"input_asset\\"],\\"outputs\\":[\\"facts\\"],\\"activation\\":\\"input_asset\\",\\"budget\\":1,\\"tool_scope\\":[],\\"labels\\":[]}]}"}}',
        "",
    ]


def _planning_stage_instructions(contract: Contract) -> list[str]:
    stages = {
        "plugin:plan.draft": (
            "Planning draft protocol (required):",
            "Return the declared output as JSON with goals, evidence_needs, "
            "uncertainties, and proposed_steps. Do not emit Contract wire objects.",
        ),
        "plugin:replan.draft": (
            "Replanning draft protocol (required):",
            "Return the declared output as JSON with invalidated_assumptions, "
            "reachable_evidence, uncertainties, and proposed_successors.",
        ),
        "plugin:plan.dependencies": (
            "Planning dependency protocol (required):",
            "Return JSON with producers, consumers, missing_inputs, cycles, "
            "parallel_groups, capability_needs, authority_risks, and allowance_needs.",
        ),
        "plugin:replan.dependencies": (
            "Replanning dependency protocol (required):",
            "Return JSON with invalidated_edges, producers, consumers, missing_inputs, "
            "cycles, capability_needs, authority_risks, and allowance_needs.",
        ),
        "plugin:plan.compile": (
            "Planning compiler protocol (required):",
            "Return /exec with exactly one temporary output named planning_blueprint; "
            "its content is one JSON object with a contracts array. Every contract "
            "must include a non-empty executable description and non-empty outputs; "
            "together they must produce every required parent output, with inputs and "
            "activation wired to preceding outputs. The Worker host compiles it to "
            "child declarations, so it is not committed as an Asset. Use only facts "
            "present in the draft and dependency analysis.",
        ),
        "plugin:replan.compile": (
            "Replanning compiler protocol (required):",
            "Return /exec with exactly one temporary output named planning_blueprint; "
            "its content is one JSON object with a contracts array of successors. Every "
            "successor must include a non-empty executable description and non-empty "
            "outputs; together they must re-commit all required parent outputs with "
            "explicit input/output wiring. The Worker host compiles it to declarations; "
            "never mutate prior Contracts.",
        ),
    }
    for label in contract.labels:
        instruction = stages.get(label)
        if instruction is not None:
            lines = [instruction[0], f"- {instruction[1]}"]
            if label in {
                "plugin:plan.draft",
                "plugin:replan.draft",
                "plugin:plan.dependencies",
                "plugin:replan.dependencies",
            }:
                output = contract.outputs[0] if contract.outputs else "declared_output"
                lines.extend(
                    [
                        f"- Return /exec with exactly one output named `{output}`.",
                        "- Its output content must be a JSON object serialized as a string value; do not return a bare JSON object or markdown.",
                    ]
                )
            if label in {"plugin:plan.compile", "plugin:replan.compile"}:
                allowed_labels = [
                    item for item in contract.labels if not item.startswith("plugin:")
                ]
                lines.extend(
                    [
                        "- Each contract object must contain name, description, inputs, outputs, activation, budget, tool_scope, labels, capability_needs, pool_needs, delegation_capabilities, and delegation_pools.",
                        "- name and description must be non-empty; outputs must contain at least one asset name.",
                        f"- Sum of child budgets must be at most {contract.budget}; use budget 1 per child unless more is essential.",
                        f"- Each child labels list must be a subset of {json.dumps(allowed_labels, ensure_ascii=False)}; never invent labels or copy plugin:* labels.",
                        "- capability_needs are execution requirements, not prompt labels.",
                        f"- Each child capability_needs and delegation_capabilities must be subsets of {json.dumps(list(contract.delegation_capabilities), ensure_ascii=False)}.",
                        f"- Each child pool_needs and delegation_pools must be subsets of {json.dumps(list(contract.delegation_pools), ensure_ascii=False)}.",
                        f"- The child outputs must collectively include: {', '.join(contract.outputs)}.",
                        f"- Valid example for this exact Contract: {_planning_compile_example(contract)}",
                    ]
                )
            lines.append("")
            return lines
    return []


def _planning_compile_example(contract: Contract) -> str:
    try:
        description = json.loads(contract.description)
    except json.JSONDecodeError:
        description = {}
    raw_inputs = description.get("allowed_inputs", contract.inputs)
    if not isinstance(raw_inputs, (list, tuple)):
        raw_inputs = contract.inputs
    allowed_inputs = [name for name in raw_inputs if isinstance(name, str)]
    required_outputs = list(contract.outputs) or ["required_output"]
    intermediate = "grounded_findings"
    first_activation = " AND ".join(allowed_inputs)
    if contract.budget <= 1:
        contracts = [
            {
                "name": "produce_required_output",
                "description": "Produce the required output from the disclosed inputs.",
                "inputs": allowed_inputs,
                "outputs": required_outputs,
                "activation": first_activation,
                "budget": 1,
                "tool_scope": [],
                "labels": [],
            }
        ]
    else:
        contracts = [
            {
                "name": "extract_grounded_findings",
                "description": "Extract only grounded findings from the disclosed inputs.",
                "inputs": allowed_inputs,
                "outputs": [intermediate],
                "activation": first_activation,
                "budget": 1,
                "tool_scope": [],
                "labels": [],
            },
            {
                "name": "produce_required_output",
                "description": "Produce the required output from grounded findings.",
                "inputs": [intermediate],
                "outputs": required_outputs,
                "activation": intermediate,
                "budget": 1,
                "tool_scope": [],
                "labels": [],
            },
        ]
    for item in contracts:
        item.update(
            capability_needs=[],
            pool_needs=[],
            delegation_capabilities=[],
            delegation_pools=[],
        )
    return json.dumps(
        {"contracts": contracts},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _generated_task_instructions(contract: Contract) -> list[str]:
    """Give ordinary generated tasks an exact, non-recursive output contract."""
    if "plugin:fail" in contract.labels:
        return [
            "Failure reporting task protocol (required):",
            "- This task records an already-declared failure; do not use /fail again.",
            "- Return /exec with exactly the declared output name.",
            "- The output content must be a concise JSON failure report preserving the reason in the task description.",
            "",
        ]
    if "plugin:retry" in contract.labels:
        return [
            "Retry task protocol (required):",
            "- Execute the original task from the disclosed inputs and return /exec with exactly its declared outputs.",
            "- Do not request another /retry unless a new transient failure actually occurs and allowance remains.",
            "",
        ]
    if contract.origin == "recovery":
        return [
            "Recovery task protocol (required):",
            "- Repair the failed candidate using the disclosed failure context.",
            "- Return /exec with exactly the declared output names; do not request another recovery action.",
            "",
        ]
    return []
