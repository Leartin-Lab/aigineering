"""Prompt construction for candidate-producing workers."""

from __future__ import annotations

from aigineering.core.labels import BEHAVIOR_LABEL_PREFIX
from aigineering.protocol.types import Asset, Contract


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
        '`/tool {"name": "...", "args": {}}`. If the same task can be '
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
    evidence_assets = [
        asset for asset in assets if not asset.name.startswith(BEHAVIOR_LABEL_PREFIX)
    ]

    lines = [
        f"Contract name: {contract.name}",
        f"Description: {contract.description}",
        "Declared inputs: " + ", ".join(contract.inputs),
        "Declared outputs: " + ", ".join(contract.outputs),
        "Allowed tools: " + ", ".join(contract.tool_scope),
        "",
        "Return format:",
        '- /exec {"outputs": {"declared_output": "content"}}',
        '- /plan {"reason": "why the current task needs more information"}',
        '- /replan {"reason": "why the current task has already gone off course"}',
        '- /tool {"name": "tool_name", "args": {}}',
        '- /retry {"reason": "transient failure that permits the same task to be attempted again"}',
        '- /fail {"reason": "why the task cannot be completed safely"}',
        "",
        "Decision boundary:",
        "- Use /plan when disclosed information is insufficient.",
        "- Use /replan only after an assumption, path, or result is invalid.",
        "- Do not use /replan for missing information.",
        "- Use /retry only for a transient execution or output failure when the same task can be attempted again.",
        "- Do not use /retry when new evidence or a different plan is required.",
        "- Use /fail when evidence cannot be obtained safely or completion would require fabricated facts.",
        "",
    ]
    lines.extend(_method_result_instructions(contract))
    lines.extend(_planning_stage_instructions(contract))
    lines.extend(
        [
            "Behavior instructions:",
        ]
    )
    for asset in behavior_assets:
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
        "- Each child may contain only: name, description, inputs, outputs, activation, budget, tool_scope, labels.",
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
            "its content is one JSON object with a contracts array. The Worker host "
            "compiles it to child declarations, so it is not committed as an Asset. "
            "Use only facts present in the draft and dependency analysis.",
        ),
        "plugin:replan.compile": (
            "Replanning compiler protocol (required):",
            "Return /exec with exactly one temporary output named planning_blueprint; "
            "its content is one JSON object with a contracts array of successors. "
            "The Worker host compiles it to declarations; never mutate prior Contracts.",
        ),
    }
    for label in contract.labels:
        instruction = stages.get(label)
        if instruction is not None:
            return [instruction[0], f"- {instruction[1]}", ""]
    return []
