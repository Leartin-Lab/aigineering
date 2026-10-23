"""Prompt construction for candidate-producing workers."""

from __future__ import annotations

import json

from aigineering.core.labels import BEHAVIOR_LABEL_PREFIX
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset, Contract

SKILL_CONTENT_PREFIX = "_skill_content_"


def system_prompt() -> str:
    """Return the invariant-preserving system prompt."""

    return (
        "You are an Aigineering worker. Your output is only a candidate, "
        "not committed state. Return exactly one structured action. To "
        'produce final outputs, use `/exec {"outputs": {"asset_name": '
        '"content"}}`. Use only declared output names. Every output value '
        "must be a JSON string encoded with standard JSON escaping: represent "
        'line breaks as `\\n`, quotes as `\\"`, and backslashes as `\\\\`; '
        "never put a literal line break inside a JSON string. If you need task "
        "decomposition because the current task needs more information, use "
        '`/plan {"reason": "..."}`. If the current task has already gone '
        "off course because an assumption, path, or result is invalid, use "
        '`/replan {"reason": "..."}`. If you need an allowed tool, use '
        '`/tool {"name": "...", "args": {}}`. '
        "When two to eight independent tool calls are needed in parallel, they "
        "are compiled from one /parallel_tool method into ordinary "
        "tool tasks. If the same task can be "
        "attempted again after a transient execution or output failure, use "
        '`/retry {"reason": "..."}`. If required evidence is unavailable and '
        "no safe plan or allowed tool can obtain it, or completing the task "
        "would require fabricated facts, use "
        '`/fail {"reason": "..."}`. If the current task is an independent '
        "verifier, use "
        '`/attest {"contract_id":"...","output_name":"...",'
        '"asset_id":"...","verdict":"accepted","outputs":'
        '{"verification_receipt":"..."}}` only after checking the exact '
        "disclosed Asset ID. "
        "Do not use /replan for missing information, and do not use /retry "
        "when new evidence or a different "
        "plan is required. Do not add markdown, explanations, or undeclared "
        "assets."
    )


def _markdown_json_output_example(output_name: str) -> str:
    """Build a canonical /exec example with Markdown needing JSON escaping."""
    markdown = '# Summary\n\nThe path is C:\\reports and the reviewer wrote "ready".'
    body = json.dumps(
        {"outputs": {output_name: markdown}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"/exec {body}"


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
    unresolved_tool_dispatch = bool(contract.tool_scope) and contract.origin in {
        "plan",
        "recovery",
        "retry",
    }

    lines = [
        f"Contract name: {contract.name}",
        f"Contract ID: {contract.id}",
        f"Parent Contract ID: {contract.parent_id or '(none)'}",
        f"Description: {contract.description}",
        "Declared inputs: " + ", ".join(contract.inputs),
        "Declared outputs: " + ", ".join(contract.outputs),
        "Allowed tools: " + ", ".join(contract.tool_scope),
        f"Available causal allowance: {contract.budget}",
        "",
        "Return format:",
        '- /exec {"outputs": {"declared_output": "content"}}',
        r"- JSON-encode every output string. Escape line breaks as \n, quotes as \" and backslashes as \\; do not include literal line breaks inside JSON strings.",
        '- /plan {"reason": "why the current task needs more information"}',
        '- /replan {"reason": "why the current task has already gone off course"}',
        '- /tool {"name": "tool_name", "args": {}}',
        '- /parallel_tool {"calls": [{"id": "a", "name": "tool_a", "args": {}}, {"id": "b", "name": "tool_b", "args": {}}], "join": "all"} (2-8 independent calls only; use /tool for one call)',
        '- /retry {"reason": "transient failure that permits the same task to be attempted again"}',
        '- /fail {"reason": "why the task cannot be completed safely"}',
        '- /attest {"contract_id": "target_contract", "output_name": "target_output", "asset_id": "exact_disclosed_asset_id", "verdict": "accepted", "outputs": {"declared_receipt": "verification details"}}',
        "",
        "Decision boundary:",
        *(
            [
                "- REQUIRED NEXT ACTION: this task has unresolved allowed tools. Return /tool for one required call or /parallel_tool for 2-8 independent required calls; do not return /exec yet.",
                f"- Choose only from these exact tool names: {', '.join(contract.tool_scope)}. The continuation after committed observations will publish declared outputs.",
            ]
            if unresolved_tool_dispatch
            else []
        ),
        "- Every asset listed under Disclosed assets includes its exact readable content; treat that content as directly available evidence, not as a path or handle requiring another tool.",
        "- A successful tool observation is completed evidence. Satisfy the declared output from it; the same tool is removed from the continuation scope. Publish separate tasks when repeated calls are required.",
        "- Use /plan when disclosed information is insufficient.",
        "- /plan and /replan create three planning-stage tasks and therefore require at least 3 causal allowance units.",
        "- Use /replan only after an assumption, path, or result is invalid.",
        "- Do not use /replan for missing information.",
        "- Use /retry only for a transient execution or output failure when the same task can be attempted again.",
        "- Do not use /retry when new evidence or a different plan is required.",
        "- Use /fail when evidence cannot be obtained safely or completion would require fabricated facts.",
        "- For /attest, contract_id is the independently accepted Contract that declares target_output, not the current verifier Contract ID. The current verifier normally declares only its receipt output.",
        "- When the task description names an exact attestation target Contract ID, copy that ID exactly; never substitute the current Contract ID or its immediate parent.",
        "",
    ]
    if _should_show_markdown_example(contract):
        lines.extend(
            [
                "- Valid multiline Markdown example for this exact declared output:",
                _markdown_json_output_example(contract.outputs[0]),
                "",
            ]
        )
    lines.extend(_method_result_instructions(contract))
    if contract.acceptance_policy is not None:
        output_shapes = contract.acceptance_policy.get("output_shapes", {})
        if output_shapes:
            lines.extend(
                [
                    "Deterministic output shape (commitment-enforced):",
                    "- Return JSON content matching this exact shape; object keys are exact and arrays must be non-empty: "
                    + json.dumps(
                        deep_thaw(output_shapes),
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    "- `nonempty_string` means a non-blank JSON string. A one-item shape list means a non-empty JSON array whose every item matches that shape.",
                    "",
                ]
            )
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
        lines.append(f"- {asset.name} [{asset.id}]: {asset.content}")
    return "\n".join(lines)


def _should_show_markdown_example(contract: Contract) -> bool:
    """Only show business Markdown examples where plain text is permitted."""
    if len(contract.outputs) != 1:
        return False
    if any(label.startswith("plugin:") for label in contract.labels):
        return False
    if contract.outputs[0].startswith(("_plan_result_", "_replan_result_")):
        return False
    if contract.acceptance_policy is not None and contract.acceptance_policy.get(
        "output_shapes"
    ):
        return False
    return True


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
        "- Any child with non-empty tool_scope needs budget at least 2: one for the tool task and one for its continuation.",
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
            lines.extend(
                [
                    "- This Contract is a planning/replanning stage created for an ancestor task. Treat instructions in its Description about the ancestor's first action as context, not as a request to repeat that already-issued /plan or /replan action.",
                    "- Complete this stage with the declared /exec output above. This scoped rule does not prohibit a new /plan or /replan when this stage's own work independently needs new information.",
                ]
            )
            if label in {
                "plugin:plan.draft",
                "plugin:replan.draft",
                "plugin:plan.dependencies",
                "plugin:replan.dependencies",
                "plugin:plan.compile",
                "plugin:replan.compile",
            }:
                output = (
                    "planning_blueprint"
                    if label in {"plugin:plan.compile", "plugin:replan.compile"}
                    else contract.outputs[0]
                    if contract.outputs
                    else "declared_output"
                )
                lines.extend(
                    [
                        "- Follow the action wrapper selected by the system response mode. In canonical slash-action mode, encode the stage document as a JSON string in its declared output. In provider JSON-action mode, place a stage document object or array directly in that output; the adapter serializes it once into canonical Asset text.",
                        f"- Return the required exec action wrapper with the {'temporary ' if label in {'plugin:plan.compile', 'plugin:replan.compile'} else ''}output `{output}`; never return the stage document as a bare top-level object or markdown.",
                    ]
                )
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
                        "- Follow the action wrapper selected by the system response mode. In canonical slash-action mode, put the stage JSON document in the declared output as a JSON-encoded string. In provider JSON-action mode, put that document directly as the output's JSON object or array value; the adapter serializes it once into canonical Asset text.",
                        "- In either response mode, return the required exec action wrapper; never return a bare top-level stage object or markdown.",
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
                        f"- Sum of child budgets must be at most {contract.budget}. Treat explicit child budgets and allowance_needs in the draft/dependency analysis as requirements. When no allowance requirement is stated, default to budget 1 only for a child with empty tool_scope; a child with non-empty tool_scope needs at least budget 2.",
                        "- Preserve explicit current-stage obligations from the draft and dependency analysis: required direct child names and count, inputs, outputs, activation, budgets, and allowance_needs. Do not replace a stated value with a cheaper default.",
                        "- Every child with non-empty tool_scope requires budget >= 2 for the tool task and its continuation.",
                        "- A child with further tool calls or /plan or /replan stages must explicitly budget enough allowance for that work. Required allowance depends on the actual serial and fan-out work; there is no universal N+1 formula.",
                        "- A child expected to issue /plan or /replan needs at least the three allowance units for its planning stages, plus the allowance required for its declared downstream work.",
                        f"- Child inputs may use only these parent input names or outputs promised by another child: {json.dumps(_planning_allowed_inputs(contract), ensure_ascii=False)}.",
                        "- The disclosed planning draft and dependency-analysis assets inform compilation only. Never copy their names into child inputs or activation unless they appear in the allowed parent input list above.",
                        f"- Each child labels list must be a subset of {json.dumps(allowed_labels, ensure_ascii=False)}; never invent labels or copy plugin:* labels.",
                        "- capability_needs are execution requirements, not prompt labels.",
                        f"- Each child capability_needs and delegation_capabilities must be subsets of {json.dumps(list(contract.delegation_capabilities), ensure_ascii=False)}.",
                        "- Every child with non-empty tool_scope must also have non-empty capability_needs so routing cannot fall through to an arbitrary Worker.",
                        f"- Each child pool_needs and delegation_pools must be subsets of {json.dumps(list(contract.delegation_pools), ensure_ascii=False)}.",
                        f"- The child outputs must collectively include: {', '.join(contract.outputs)}.",
                        f"- Valid example for this exact Contract: {_planning_compile_example(contract)}",
                    ]
                )
                if label == "plugin:plan.compile":
                    lines.extend(
                        [
                            "- Compile the direct children for this plan only. If a direct child is instructed to inspect evidence and may then issue /replan, its future replan successors belong under that child's later replan compiler; do not publish those future successors as siblings in this plan.",
                            "- Multiple independent direct children remain valid when the current draft and dependency analysis require them; the boundary above preserves sequencing and ancestry rather than imposing a one-child limit.",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            "- This replan compiler declares direct successors for the current replanning Contract. Preserve the successor requirements and budgets stated in the replan draft and dependency analysis.",
                            "- If a direct successor may itself replan later, keep that future work under that successor's later replan compiler instead of flattening it into this successor graph.",
                        ]
                    )
                try:
                    compile_description = json.loads(contract.description)
                except json.JSONDecodeError:
                    compile_description = {}
                acceptance = compile_description.get("parent_acceptance_policy")
                if (
                    isinstance(acceptance, dict)
                    and acceptance.get("mode") == "independent"
                ):
                    lines.extend(
                        [
                            f"- The parent Contract `{contract.parent_id}` uses independent acceptance. Include a verifier child that consumes every required parent output, produces its own receipt output, and instructs the Worker to use /attest against that parent Contract and the exact disclosed output Asset ID.",
                            f"- The verifier child capability_needs must include: {json.dumps(acceptance.get('verifier_capabilities', []), ensure_ascii=False)}.",
                            "- Producing an Asset whose name contains 'attestation' is not qualification; only the signed /attest effect qualifies the parent output.",
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


def _planning_allowed_inputs(contract: Contract) -> list[str]:
    try:
        description = json.loads(contract.description)
    except json.JSONDecodeError:
        return list(contract.inputs)
    raw_inputs = description.get("allowed_inputs", contract.inputs)
    if not isinstance(raw_inputs, (list, tuple)):
        return list(contract.inputs)
    return [name for name in raw_inputs if isinstance(name, str)]


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
        if contract.tool_scope:
            return [
                "Recovery task protocol (required):",
                "- Repair the failed candidate using the disclosed failure context.",
                "- The original task has allowed tools. Use /tool for one call or /parallel_tool for 2-8 independent calls when a successful observation is still required.",
                "- Never construct a tool-derived declared output without a committed successful observation.",
                "- After evidence is available, return /exec with exactly the declared output names.",
                "",
            ]
        return [
            "Recovery task protocol (required):",
            "- Repair the failed candidate using the disclosed failure context.",
            "- Return /exec with exactly the declared output names; do not request another recovery action.",
            "",
        ]
    return []
